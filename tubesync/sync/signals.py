import os
import glob
from django.conf import settings
from django.db.models.signals import pre_save, post_save, pre_delete, post_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from background_task.signals import task_failed
from background_task.models import Task
from common.logger import log
from .models import Source, Media, MediaServer
from .tasks import (delete_task_by_source, delete_task_by_media, index_source_task,
                    download_media_thumbnail, download_media_metadata,
                    map_task_to_instance, check_source_directory_exists,
                    download_media, rescan_media_server, download_source_images,
                    save_all_media_for_source, get_media_metadata_task)
from .utils import delete_file
from .filtering import filter_media


@receiver(pre_save, sender=Source)
def source_pre_save(sender, instance, **kwargs):
    # Triggered before a source is saved, if the schedule has been updated recreate
    # its indexing task
    try:
        existing_source = Source.objects.get(pk=instance.pk)
    except Source.DoesNotExist:
        # Probably not possible?
        return
    if existing_source.index_schedule != instance.index_schedule:
        # Indexing schedule has changed, recreate the indexing task
        delete_task_by_source('sync.tasks.index_source_task', instance.pk)
        verbose_name = _('Index media from source "{}"')
        index_source_task(
            str(instance.pk),
            repeat=instance.index_schedule,
            queue=str(instance.pk),
            priority=5,
            verbose_name=verbose_name.format(instance.name),
            remove_existing_tasks=True
        )


@receiver(post_save, sender=Source)
def source_post_save(sender, instance, created, **kwargs):
    # Check directory exists and create an indexing task for newly created sources
    if created:
        verbose_name = _('Check download directory exists for source "{}"')
        check_source_directory_exists(
            str(instance.pk),
            priority=0,
            verbose_name=verbose_name.format(instance.name)
        )
        if instance.source_type != Source.SOURCE_TYPE_YOUTUBE_PLAYLIST and instance.copy_channel_images:
            download_source_images(
                str(instance.pk),
                priority=0,
                verbose_name=verbose_name.format(instance.name)
            )
        if instance.index_schedule > 0:
            delete_task_by_source('sync.tasks.index_source_task', instance.pk)
            log.info(f'Scheduling media indexing for source: {instance.name}')
            verbose_name = _('Index media from source "{}"')
            index_source_task(
                str(instance.pk),
                repeat=instance.index_schedule,
                queue=str(instance.pk),
                priority=5,
                verbose_name=verbose_name.format(instance.name),
                remove_existing_tasks=True
            )
    verbose_name = _('Checking all media for source "{}"')
    save_all_media_for_source(
        str(instance.pk),
        priority=0,
        verbose_name=verbose_name.format(instance.name),
        remove_existing_tasks=True
    )


@receiver(pre_delete, sender=Source)
def source_pre_delete(sender, instance, **kwargs):
    # Triggered before a source is deleted, delete all media objects to trigger
    # the Media models post_delete signal
    for media in Media.objects.filter(source=instance):
        log.info(f'Deleting media for source: {instance.name} item: {media.name}')
        media.delete()


@receiver(post_delete, sender=Source)
def source_post_delete(sender, instance, **kwargs):
    # Triggered after a source is deleted
    log.info(f'Deleting tasks for source: {instance.name}')
    delete_task_by_source('sync.tasks.index_source_task', instance.pk)


@receiver(task_failed, sender=Task)
def task_task_failed(sender, task_id, completed_task, **kwargs):
    # Triggered after a task fails by reaching its max retry attempts
    obj, url = map_task_to_instance(completed_task)
    if isinstance(obj, Source):
        log.error(f'Permanent failure for source: {obj} task: {completed_task}')
        obj.has_failed = True
        obj.save()

    if isinstance(obj, Media) and completed_task.task_name == "sync.tasks.download_media_metadata":
        log.error(f'Permanent failure for media: {obj} task: {completed_task}')
        obj.skip = True
        obj.save()

@receiver(post_save, sender=Media)
def media_post_save(sender, instance, created, **kwargs):
    # If the media is skipped manually, bail.
    if instance.manual_skip:
        return
    # Triggered after media is saved
    skip_changed = False
    can_download_changed = False
    # Reset the skip flag if the download cap has changed if the media has not
    # already been downloaded
    if not instance.downloaded and instance.metadata:
        skip_changed = filter_media(instance)

    # Recalculate the "can_download" flag, this may
    # need to change if the source specifications have been changed
    if instance.metadata:
        if instance.get_format_str():
            if not instance.can_download:
                instance.can_download = True
                can_download_changed = True
        else:
            if instance.can_download:
                instance.can_download = False
                can_download_changed = True
    # Save the instance if any changes were required
    if skip_changed or can_download_changed:
        post_save.disconnect(media_post_save, sender=Media)
        instance.save()
        post_save.connect(media_post_save, sender=Media)
    # If the media is missing metadata schedule it to be downloaded
    if not instance.metadata and not instance.skip and not get_media_metadata_task(instance.pk):
        log.info(f'Scheduling task to download metadata for: {instance.url}')
        verbose_name = _('Downloading metadata for "{}"')
        download_media_metadata(
            str(instance.pk),
            priority=5,
            verbose_name=verbose_name.format(instance.pk),
            remove_existing_tasks=True
        )
    # If the media is missing a thumbnail schedule it to be downloaded (unless we are skipping this media)
    if not instance.thumb_file_exists:
        instance.thumb = None
    if not instance.thumb and not instance.skip:
        thumbnail_url = instance.thumbnail
        if thumbnail_url:
            log.info(f'Scheduling task to download thumbnail for: {instance.name} '
                     f'from: {thumbnail_url}')
            verbose_name = _('Downloading thumbnail for "{}"')
            download_media_thumbnail(
                str(instance.pk),
                thumbnail_url,
                queue=str(instance.source.pk),
                priority=10,
                verbose_name=verbose_name.format(instance.name),
                remove_existing_tasks=True
            )
    # If the media has not yet been downloaded schedule it to be downloaded
    if not instance.media_file_exists:
        instance.downloaded = False
        instance.media_file = None
    if (not instance.downloaded and instance.can_download and not instance.skip
        and instance.source.download_media):
        delete_task_by_media('sync.tasks.download_media', (str(instance.pk),))
        verbose_name = _('Downloading media for "{}"')
        download_media(
            str(instance.pk),
            queue=str(instance.source.pk),
            priority=15,
            verbose_name=verbose_name.format(instance.name),
            remove_existing_tasks=True
        )


@receiver(pre_delete, sender=Media)
def media_pre_delete(sender, instance, **kwargs):
    # Triggered before media is deleted, delete any scheduled tasks
    log.info(f'Deleting tasks for media: {instance.name}')
    delete_task_by_media('sync.tasks.download_media', (str(instance.pk),))
    thumbnail_url = instance.thumbnail
    if thumbnail_url:
        delete_task_by_media('sync.tasks.download_media_thumbnail',
                             (str(instance.pk), thumbnail_url))
    if instance.source.delete_files_on_disk and (instance.media_file or instance.thumb):
        # Delete all media files if it contains filename
        filepath = instance.media_file.path if instance.media_file else instance.thumb.path
        barefilepath, fileext = os.path.splitext(filepath)
        # Get all files that start with the bare file path
        all_related_files = glob.glob(f'{barefilepath}.*')
        for file in all_related_files:
            log.info(f'Deleting file for: {instance} path: {file}')
            delete_file(file)


@receiver(post_delete, sender=Media)
def media_post_delete(sender, instance, **kwargs):
    # Schedule a task to update media servers
    for mediaserver in MediaServer.objects.all():
        log.info(f'Scheduling media server updates')
        verbose_name = _('Request media server rescan for "{}"')
        rescan_media_server(
            str(mediaserver.pk),
            priority=0,
            verbose_name=verbose_name.format(mediaserver),
            remove_existing_tasks=True
        )
