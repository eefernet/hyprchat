"""Keep anonymous web fetches and routine access logs free of identifiers."""
import logging
from http.cookiejar import CookieJar, DefaultCookiePolicy


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def anonymous_cookies():
    # Preserve connection pooling without retaining a site's identifier across
    # searches, redirects, profiles or VPN exits.
    return CookieJar(policy=_NoCookies())


class _PrivateAccessLog(logging.Filter):
    def filter(self, record):
        # Uvicorn's access tuple is (client, method, path, version, status).
        # Query strings include search terms, image URLs and session tokens.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if isinstance(path, str):
                record.args = (client, method, path.split('?', 1)[0], version, status)
        return True


def install_private_access_logging():
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, _PrivateAccessLog) for f in logger.filters):
        logger.addFilter(_PrivateAccessLog())
