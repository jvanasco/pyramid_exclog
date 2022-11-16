import builtins
from pprint import pformat
from pyramid.httpexceptions import WSGIHTTPException
from pyramid.settings import asbool, aslist
import pyramid.tweens
from pyramid.util import DottedNameResolver
import sys

resolver = DottedNameResolver(None)


def as_globals_list(value):
    L = []
    value = aslist(value)
    for dottedname in value:
        if dottedname in builtins.__dict__:
            dottedname = 'builtins.%s' % dottedname
        obj = resolver.maybe_resolve(dottedname)
        L.append(obj)
    return L


def _get_url(request):
    try:
        url = repr(request.url)
    except UnicodeDecodeError:
        # do the best we can
        url = (
            request.host_url
            + request.environ.get('SCRIPT_NAME')
            + request.environ.get('PATH_INFO')
        )
        qs = request.environ.get('QUERY_STRING')
        if qs:
            url += '?' + qs
        url = 'could not decode url: %r' % url
    return url


_MESSAGE_TEMPLATE = """

%(url)s

ENVIRONMENT

%(env)s


PARAMETERS

%(params)s


UNAUTHENTICATED USER

%(usr)s

"""


def _hide_cookies(cookie_keys, request):
    """
    Return a copy of the request with the specified cookies' values replaced
    with "hidden", if present.
    """

    new_request = request.copy()
    new_request.registry = request.registry
    cookies = new_request.cookies

    for key in cookie_keys:
        if key in cookies:
            cookies[key] = 'hidden'

    # This forces the cookie handler to update its parsed cookies cache, which
    # also ends up in the environ dump
    len(cookies)

    return new_request


def _get_message(request):
    """
    Return a string with useful information from the request.

    On python 2 this method will return ``unicode`` and on Python 3 ``str``
    will be returned. This seems to be what the logging module expects.

    """
    url = _get_url(request)
    unauth = request.unauthenticated_userid

    try:
        params = request.params
    except UnicodeDecodeError:
        params = 'could not decode params'
    except IOError as ex:
        params = 'IOError while decoding params: %s' % ex

    if not isinstance(unauth, str):
        unauth = repr(unauth)

    return _MESSAGE_TEMPLATE % dict(
        url=url,
        env=pformat(request.environ),
        params=pformat(params),
        usr=unauth,
    )


class ErrorHandler(object):
    def __init__(self, ignored, getLogger, get_message, hidden_cookies=()):
        self.ignored = ignored
        self.getLogger = getLogger
        self.get_message = get_message
        self.hidden_cookies = hidden_cookies

    def __call__(self, request, exc_info=None):
        # save the traceback as it may get lost when we get the message.
        # _handle_error is not in the traceback, so calling sys.exc_info
        # does NOT create a circular reference
        if exc_info is None:
            exc_info = sys.exc_info()

        if isinstance(exc_info[1], self.ignored):
            return
        try:
            if self.hidden_cookies:
                request = _hide_cookies(self.hidden_cookies, request)

            logger = self.getLogger('exc_logger')
            message = self.get_message(request)
            logger.error(message, exc_info=exc_info)
        except BaseException:
            logger.exception("Exception while logging")


def exclog_tween_factory(handler, registry):
    get = registry.settings.get

    ignored = get('exclog.ignore', (WSGIHTTPException,))
    get_message = _get_url
    if get('exclog.extra_info', False):
        get_message = _get_message
    get_message = get('exclog.get_message', get_message)
    hidden_cookies = get('exclog.hidden_cookies', ())

    getLogger = get('exclog.getLogger', 'logging.getLogger')
    getLogger = resolver.maybe_resolve(getLogger)

    handle_error = ErrorHandler(
        ignored, getLogger, get_message, hidden_cookies=hidden_cookies
    )

    def exclog_tween(request):
        try:
            response = handler(request)
            exc_info = getattr(request, 'exc_info', None)
            if exc_info is not None:
                handle_error(request, exc_info)
            return response

        except BaseException:
            handle_error(request)
            raise

    return exclog_tween


def includeme(config):
    """
    Set up am implicit :term:`tween` to log exception information that is
    generated by your Pyramid application.  The logging data will be sent to
    the Python logger named ``exc_logger``.

    This tween configured to be placed 'over' the exception view tween.  It
    will log all exceptions (even those caught by a Pyramid exception view)
    except 'http exceptions' (any exception that derives from
    ``pyramid.httpexceptions.WSGIHTTPException`` such as ``HTTPFound``).  You
    can instruct ``pyramid_exclog`` to ignore custom exception types by using
    the ``exclog.ignore`` configuration setting.

    """
    get = config.registry.settings.get
    ignored = as_globals_list(
        get(
            'exclog.ignore',
            'pyramid.httpexceptions.WSGIHTTPException',
        )
    )
    extra_info = asbool(get('exclog.extra_info', False))
    hidden_cookies = aslist(get('exclog.hidden_cookies', ''))
    get_message = get('exclog.get_message', None)
    if get_message is not None:
        get_message = config.maybe_dotted(get_message)
        config.registry.settings['exclog.get_message'] = get_message
    config.registry.settings['exclog.ignore'] = tuple(ignored)
    config.registry.settings['exclog.extra_info'] = extra_info
    config.registry.settings['exclog.hidden_cookies'] = hidden_cookies
    config.add_tween(
        'pyramid_exclog.exclog_tween_factory',
        over=[
            pyramid.tweens.EXCVIEW,
            # if pyramid_tm is in the pipeline we want to track errors caused
            # by commit/abort so we try to place ourselves over it
            'pyramid_tm.tm_tween_factory',
        ],
    )