"""
Optional Sentry error logging.

A no-op unless SENTRY_DSN is set in the environment, so local/sandbox runs (and
anyone without a Sentry project) are unaffected. Pattern per scraper:

    from obs import init_sentry, log_error
    ...
    if __name__ == "__main__":
        init_sentry(SITE)
        try:
            scrape()
        except Exception as e:
            log_error(e, site=SITE, phase="scrape")
            raise

and inside loops that currently swallow per-item errors:

    except Exception as e:
        log_error(e, site=SITE, item=slug)
        ...continue...
"""
import os

_initialized = False


def init_sentry(component=None):
    """Initialise Sentry if SENTRY_DSN is set and sentry_sdk is installed.
    Returns True when active. Safe to call repeatedly. The `component` (e.g. the
    site name) is attached as a tag so events can be filtered per scraper."""
    global _initialized
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        return False
    if not _initialized:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENV", "production"),
            traces_sample_rate=0.0,
        )
        _initialized = True
    if component:
        sentry_sdk.set_tag("component", component)
    return True


def log_error(exc, **tags):
    """Report an exception to Sentry with optional tags. No-op if Sentry isn't
    active/installed. Never raises."""
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sentry_sdk.capture_exception(exc)
    except Exception:
        try:
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
