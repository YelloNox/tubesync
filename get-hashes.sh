BGUTIL_YTDLP_POT_PROVIDER_VERSION="2.0.2"
curl -fsSL -o /tmp/bgutil.tar.gz \
  "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${BGUTIL_YTDLP_POT_PROVIDER_VERSION}.tar.gz"
echo "BGUTIL_YTDLP_POT_PROVIDER_SHA256: $(sha256sum /tmp/bgutil.tar.gz)"