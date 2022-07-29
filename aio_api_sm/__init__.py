"""AIO Request Manager"""
import time
import json
import pprint
import asyncio
import aiohttp
import logging
import functools

log = logging.getLogger(__name__)

class RequestManagerError(RuntimeError):
    pass

class RetriesExceededError(RequestManagerError):
    pass

class MaxRequestsExceededError(RequestManagerError):
    pass

def default_retry(exception):
    """A simple callable that retries on some 5XX status codes.

    Define your own callable that returns True if should retry, else False.

    :param exception: the exception raised during the request.
    :type exception: Exception
    :returns: True if should retre, else False
    """
    retry_for_statuses = [500, 502, 503, 504, 599]

    if isinstance(exception, aiohttp.ClientResponseError):
        if exception.status in retry_for_statuses:
            return True
    return False

def exponential_backoff_with_jitter(attempt):
    """A simple exponential backoff with jitter callable.

    Accepts the number of attempts as an argument and returns a backoff time.

    :param attempt: the number of failed attempts so far.
    :type attempt: int
    :returns: seconds to sleep before next request.
    :rtype: int or float
    """
    base = 2
    max_sleep = 10
    millis_to_secs = 100000
    jitter = random.randint(0, 1000)
    return min((jitter * base ** attempt)/millis_to_secs, max_sleep)


class AioApiSessionManager():
    """An async, retying, rate limited HTTP session manager.

    :param api_base: the root of the API host to connect to.
    :type api_base: str
    :param headers: headers to add to the request.
    :type headers: dict
    :param should_retry: a callable that accepts an exception and returns
                         True if the call should be retried. Set to None
                         to disable retries.
                         Default: aio_api_sm.default_retry
    :type should_retry: function
    :param retries: maximum number of retries for a request. Default 3.
    :type retries: int
    :param backoff: a callable that accepts the current number of attempts
                    and returns the time to sleep before retrying.
                    Set to None to disable backoff.
                    Default: aio_api_sm.exponential_backoff_with_jitter
    :type backoff: function
    :param rate_limit: maximum number of request per second. Default: 3.
                       Disable with None or 0.
    :type rate_limit: int
    :param rate_limit_burst: maximum rate limit burst size.
    :type rate_limit_burst: int
    :param limit_per_host: maximum number of connections to the host.
                           Default: 10
    :type limit_per_host: int
    :param ttl_dns_cache: time to live for cached dns records. Default: 300s
    :type ttl_dns_cache: int
    """

    # users can call AioApiSessionManager.<shortcut> to execute a verb
    shortcuts = ['delete', 'get', 'head', 'options', 'patch', 'post', 'put']

    # minimum queue length
    rate_limit_min_burst_size = 2

    # minimum sleep for rate manager task between wakes
    min_sleep = 0.1

    # how long to pause if we get a malformed 429
    default_retry_after_delay = 5

    def __init__(self, api_base, headers=None, should_retry=default_retry,
                 retries=3, backoff=exponential_backoff_with_jitter,
                 rate_limit=3, rate_limit_burst=20,
                 limit_per_host=10, ttl_dns_cache=300,
                 json_serialize=json.dumps, json_deserialize=json.loads,
                 **session_kwargs):
        self.api_base = api_base
        self.headers = headers
        self.retries = retries
        self.backoff = backoff
        self.should_retry = should_retry
        self.limit_per_host = limit_per_host
        self.ttl_dns_cache = ttl_dns_cache
        self.json_serialize = json_serialize
        self.json_deserialize = json_deserialize
        self.session_kwargs = session_kwargs

        self.connector = aiohttp.TCPConnector(
                    limit_per_host=self.limit_per_host,
                    ttl_dns_cache=self.ttl_dns_cache)

        self.retry_after_event = asyncio.Event()
        self.retry_after_event.set()
        self.retry_after_time = None

        self.rate_limit = rate_limit
        # if the max burst size is smaller than the rate limit, use the
        # rate limit as the max burst size.
        self.rate_limit_burst = max(rate_limit, rate_limit_burst)

        if self.rate_limit:
            self._token_queue = asyncio.Queue(int(rate_limit_burst))
            self._rate_manager_task = asyncio.create_task(self._rate_manager())

        self.__session = None
        self._start = time.monotonic()
        self._requests = 0

    @property
    def session(self):
        """Get the single instance of an aiohttp.ClientSession for this manager

        :returns session: returns the aiohttp.ClientSession for this manager.
        :rtype: aiohttp.ClientSession
        """
        if not self.__session:
           self.__session = aiohttp.ClientSession(
                    self.api_base,
                    connector=self.connector,
                    json_serialize=self.json_serialize,
                    **self.session_kwargs)
        return self.__session

    async def close(self):
        """Close the Request Manager and underlying aiohttp.ClientSession.

        You must call this before the Request Manager object is destroyed
        or warnings will be emitted by aio.

        :returns: None
        """
        secs = time.monotonic() - self._start
        log.info(f"{self._requests} in {secs}s {self._requests/secs}req/s")
        if self._rate_manager != None:
            self._rate_manager_task.cancel()
        if self.__session and not self.__session.closed:
            try:
                await self.__session.close()
            except:
                pass
        self.__session = None

    def _sleep_duration(self):
        """Get the loop sleep duration.

        It is generally 1/rate limit with a minimum of 0.1.
        :returns: int or None is rate limiting isn't enabled.
        """
        if self.rate_limit != None:
            return max(1/self.rate_limit, self.min_sleep)
        return None

    async def _rate_manager(self):
        """A background task that fills our leaky bucket with tokens.

        The rate manager is automatically started when a new RequestsManager
        is initialized.  It sticks token in the bucket to help rate limit
        traffic.

        :returns: None
        """
        try:
            # if we don't have a queue or a rate limit,  notion to do.
            if not (self.rate_limit and self._token_queue):
                log.warning(f"Rate limit or token queue not set, no limiting.")
                return

            sleep = self._sleep_duration()
            last_check_end = time.monotonic()
            log.debug(f'starting rate manager {self.rate_limit}')

            # start with a burst capable queue
            for i in range(0, self._token_queue.maxsize):
                self._token_queue.put_nowait(i)

            # manage the rate
            while True:
                now = time.monotonic()

                # if the retry after time has been set after a 429
                # check to see if it has expired, if so, set the event.
                if self.retry_after_time:
                    if now >= self.retry_after_time:
                        self.retry_after_event.set()
                        self.retry_after_time = None
                        log.debug(f'retry time event set, time to go!')
                    else:
                        retry_wake_in = self.retry_after_time - time.monotonic()
                        log.warn(f'retry event unset. wake in {retry_wake_in}')
                        await asyncio.sleep(retry_wake_in)

                # if the bucket isn't overflowing and we aren't in retry wait
                if not (self._token_queue.full() or self.retry_after_time):
                    # we get rate_limit tokens per second
                    # this implementation is crude, but works.
                    time_tokens = self.rate_limit * (now - last_check_end)

                    # don't add more tokens than the bucket can handle
                    max_tokens = (self._token_queue.maxsize - \
                                    self._token_queue.qsize())
                    new_tokens = int(min(time_tokens, max_tokens))

                    log.debug(f'adding {new_tokens} new tokens!')

                    # add the tokens
                    for i in range(0, new_tokens):
                        self._token_queue.put_nowait(i)

                    log.debug(f'refilled queue {self._token_queue.qsize()}')

                # store the time and sleep the interval.
                last_check_end = now
                await asyncio.sleep(sleep)
        except asyncio.CancelledError:
            log.debug('Rate Manager Cancelled')
        except Exception as err:
            log.error(f'error in rate manager: {err}')

    async def _get_token(self):
        """Get a token from the bucket.

        Tokens are only given out when there are enough (we have rate limit
        space) and we are not in a Retry-After wait.

        Calls to get_token will only return when both are true if rate limiting
        is enabled, else it will always return immediately.

        :returns: next token
        """
        # tokens are free if we aren't limited
        if self.max_requests and self._requests == self.max_requests:
            raise MaximumRequestsExceeded(
                    f'Used {self_requests} of {self.max_requests}. No more.')

        if self.rate_limit:
            # if we are in the penalty box, wait for the event
            await self.retry_after_event.wait()

            if self._token_queue != None:
                await self._token_queue.get()
                self._token_queue.task_done()
                log.debug(f'took token. remaining {self._token_queue.qsize()}')

        return None

    async def request(self, method, path, *args, **kwargs):
        """Send a request with max requests, rate limiting, and retry.

        :param method: the HTTP method to use (get, post, put, etc.)
        :type method: str
        :param path: the path (/path) of the API endpoint
        :type path: str
        :param args: args passed to aiohttp.request.
        :type args: varying
        :param kwargs: kwargs passed to aiohttp.
        :type kwargs: key value pairs

        :raises: aiohttp.ClientError if 4XX HTTP return,
                 RetriesExceededError,
                 MaxRequestsExceededError
        :returns: HTTP response
        :rtype: aiohttp.ClientResponse
        """
        request_content = kwargs.get('json', {})

        log.debug("{} to {}: \n{}".format(
            method, path, pprint.pformat(request_content)))

        requests = 0

        while requests < self.retries:
            requests += 1
            self._requests += 1
            try:
                await self._get_token()
                log.debug(f'{method} {path}: started')
                meth = getattr(self.session, method)
                resp = await meth(path, headers=self.headers, *args, **kwargs)

                resp_json = await resp.json(loads=self.json_deserialize)

                if resp.status == 429:
                    retry_after = resp.headers.get(
                            'Retry-After', self.default_retry_after_delay)
                    retry_after_secs = self._parse_retry_after(retry_after)
                    self.retry_after_time = time.monotonic() + retry_after_secs
                    self.retry_after_event.clear()
                    log.warning(
                            f"{method} {path}: 429 sleep {retry_after_secs}s")
                else:
                    resp.raise_for_status()
                    return resp_json
            except Exception as e:
                log.error("Request {} to {} failed: {}.".format(
                    method, path, e))
                if not (self.should_retry or self.should_retry(e)):
                    raise
                # we should retry, so do the backoff bit.
                if self.backoff:
                    backoff_time = self.backoff(requests)
                    log.debug(f'{method} {path}: backoff {backoff_time}s')
                    await asyncio.sleep(backoff_time)
            finally:
                log.debug(f'finished {method} {path}')

        raise RetriesExceededError("Maximum retries exceeded.")

    def _parse_retry_after(self, value):
        """Retry after headers can be seconds or timestamps, parse accordingly.

        :returns: seconds to retry after.
        :rtype: int
        """
        try:
            seconds = int(value)
            return seconds
        except ValueError:
            pass

        try:
            date = parser.parse(value)
            utcnow = datetime.now(timezone.utc)
            seconds = (date - utcnow).seconds
            return seconds
        except parser.ParserError as e:
            pass

        return self.default_retry_after_delay

    def __getattr__(self, attr):
        """Pass the shortcut http verb functions as a partial to request."""
        if attr in self.shortcuts:
            return functools.partial(self.request, attr)
        return AttributeError(
                f'{attr} doesnt exist on {self} or in {self.shortcuts}')
