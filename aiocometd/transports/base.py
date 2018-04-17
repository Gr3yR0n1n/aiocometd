"""Base transtport abstract class definition"""
import asyncio
import logging
from http import HTTPStatus
from abc import abstractmethod

import aiohttp

from .abc import Transport
from .constants import MetaChannel, TransportState, META_CHANNEL_PREFIX, \
    SERVICE_CHANNEL_PREFIX, HANDSHAKE_MESSAGE, CONNECT_MESSAGE, \
    DISCONNECT_MESSAGE, SUBSCRIBE_MESSAGE, UNSUBSCRIBE_MESSAGE, \
    PUBLISH_MESSAGE
from ..utils import get_error_code
from ..exceptions import TransportInvalidOperation


LOGGER = logging.getLogger(__name__)


class TransportBase(Transport):
    """Base transport implementation

    This class contains most of the transport operations implemented, it can
    be used as a base class for various concrete transport implementations.
    When subclassing, at a minimum the :meth:`_send_final_payload` and
    :obj:`~Transport.connection_type` methods should be reimplemented.
    """
    #: Timeout to give to HTTP session to close itself
    _HTTP_SESSION_CLOSE_TIMEOUT = 0.250

    def __init__(self, *, endpoint, incoming_queue, client_id=None,
                 reconnection_timeout=1, ssl=None, extensions=None, auth=None,
                 loop=None):
        """
        :param str endpoint: CometD service url
        :param asyncio.Queue incoming_queue: Queue for consuming incoming event
                                             messages
        :param str client_id: Clinet id value assigned by the server
        :param reconnection_timeout: The time to wait before trying to \
        reconnect to the server after a network failure
        :type reconnection_timeout: None or int or float
        :param ssl: SSL validation mode. None for default SSL check \
        (:func:`ssl.create_default_context` is used), False for skip SSL \
        certificate validation, \
        `aiohttp.Fingerprint <https://aiohttp.readthedocs.io/en/stable/\
        client_reference.html#aiohttp.Fingerprint>`_ for fingerprint \
        validation, :obj:`ssl.SSLContext` for custom SSL certificate \
        validation.
        :param extensions: List of protocol extension objects
        :type extensions: list[Extension] or None
        :param AuthExtension auth: An auth extension
        :param loop: Event :obj:`loop <asyncio.BaseEventLoop>` used to
                     schedule tasks. If *loop* is ``None`` then
                     :func:`asyncio.get_event_loop` is used to get the default
                     event loop.
        """
        #: queue for consuming incoming event messages
        self.incoming_queue = incoming_queue
        #: CometD service url
        self._endpoint = endpoint
        #: event loop used to schedule tasks
        self._loop = loop or asyncio.get_event_loop()
        #: clinet id value assigned by the server
        self._client_id = client_id
        #: message id which should be unique for every message during a client
        #: session
        self._message_id = 0
        #: reconnection advice parameters returned by the server
        self._reconnect_advice = {}
        #: set of subscribed channels
        self._subscriptions = set()
        #: boolean to mark whether to resubscribe on connect
        self._subscribe_on_connect = False
        #: current state of the transport
        self._state = TransportState.DISCONNECTED
        #: asyncio connection task
        self._connect_task = None
        #: time to wait before reconnecting after a network failure
        self._reconnect_timeout = reconnection_timeout
        #: asyncio event, set when the state becomes CONNECTED
        self._connected_event = asyncio.Event()
        #: asyncio event, set when the state becomes CONNECTING
        self._connecting_event = asyncio.Event()
        #: SSL validation mode
        self.ssl = ssl
        #: http session
        self._http_session = None
        #: List of protocol extension objects
        self._extensions = extensions or []
        #: An auth extension
        self._auth = auth

    async def _get_http_session(self):
        """Factory method for getting the current HTTP session

        :return: The current session if it's not None, otherwise it creates a
                 new session.
        """
        # it would be nicer to create the session when the class gets
        # initialized, but this seems to be the right way to do it since
        # aiohttp produces log messages with warnings that a session should be
        # created in a coroutine
        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _close_http_session(self):
        """Close the http session if it's not already closed"""
        # graceful shutdown recommended by the documentation
        # https://aiohttp.readthedocs.io/en/stable/client_advanced.html\
        # #graceful-shutdown
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            await asyncio.sleep(self._HTTP_SESSION_CLOSE_TIMEOUT)

    @property
    def connection_type(self):
        """The transport's connection type"""
        return None  # pragma: no cover

    @property
    def endpoint(self):
        """CometD service url"""
        return self._endpoint

    @property
    def client_id(self):
        """clinet id value assigned by the server"""
        return self._client_id

    @property
    def state(self):
        """Current state of the transport"""
        return self._state

    @property
    def subscriptions(self):
        """Set of subscribed channels"""
        return self._subscriptions

    async def wait_for_connected(self):
        await self._connected_event.wait()

    async def wait_for_connecting(self):
        await self._connecting_event.wait()

    async def handshake(self, connection_types):
        """Executes the handshake operation

        :param list[ConnectionType] connection_types: list of connection types
        :return: Handshake response
        :rtype: dict
        :raises TransportError: When the network request fails.
        """
        return await self._handshake(connection_types, delay=None)

    async def _handshake(self, connection_types, delay=None):
        """Executes the handshake operation

        :param list[ConnectionType] connection_types: list of connection types
        :param delay: Initial connection delay
        :type delay: None or int or float
        :return: Handshake response
        :rtype: dict
        :raises TransportError: When the network request fails.
        """
        if delay:
            await asyncio.sleep(delay, loop=self._loop)
        # reset message id for a new client session
        self._message_id = 0

        connection_types = list(connection_types)
        # make sure that the supported connection types list contains this
        # transport
        if self.connection_type not in connection_types:
            connection_types.append(self.connection_type)
        connection_type_values = [ct.value for ct in connection_types]

        # send message and await its response
        response_message = await self._send_message(
            HANDSHAKE_MESSAGE.copy(),
            supportedConnectionTypes=connection_type_values
        )
        # store the returned client id or set it to None if it's not in the
        # response
        if response_message["successful"]:
            self._client_id = response_message.get("clientId")
            self._subscribe_on_connect = True
        return response_message

    def _finalize_message(self, message):
        """Update the ``id``, ``clientId`` and ``connectionType`` message
        fields as a side effect if they're are present in the *message*.

        :param dict message: Outgoing message
        """
        if "id" in message:
            message["id"] = str(self._message_id)
            self._message_id += 1

        if "clientId" in message:
            message["clientId"] = self.client_id

        if "connectionType" in message:
            message["connectionType"] = self.connection_type.value

    def _finalize_payload(self, payload):
        """Update the ``id``, ``clientId`` and ``connectionType`` message
        fields in the *payload*, as a side effect if they're are present in
        the *message*. The *payload* can be either a single message or a list
        of messages.

        :param payload: A message or a list of messages
        :type payload: dict or list[dict]
        """
        if isinstance(payload, list):
            for item in payload:
                self._finalize_message(item)
        else:
            self._finalize_message(payload)

    async def _send_message(self, message, **kwargs):
        """Send message to server

        :param dict message: A message
        :param kwargs: Optional key-value pairs that'll be used to update the \
        the values in the *message*
        :return: Response message
        :rtype: dict
        :raises TransportError: When the network request fails.
        """
        message.update(kwargs)
        return await self._send_payload_with_auth([message])

    async def _send_payload_with_auth(self, payload):
        """Finalize and send *payload* to server and retry on authentication
        failure

        Finalize and send the *payload* to the server and return once a
        response message can be provided for the first message in the
        *payload*.

        :param list[dict] payload: A list of messages
        :return: The response message for the first message in the *payload*
        :rtype: dict
        :raises TransportError: When the network request fails.
        """
        response = await self._send_payload(payload)

        # if there is an auth extension and the response is an auth error
        if self._auth and self._is_auth_error_message(response):
            # then try to authenticate and resend the payload
            await self._auth.authenticate()
            return await self._send_payload(payload)

        # otherwise return the response
        return response

    async def _send_payload(self, payload):
        """Finalize and send *payload* to server

        Finalize and send the *payload* to the server and return once a
        response message can be provided for the first message in the
        *payload*.

        :param list[dict] payload: A list of messages
        :return: The response message for the first message in the *payload*
        :rtype: dict
        :raises TransportError: When the network request fails.
        """
        self._finalize_payload(payload)
        headers = {}
        # process the outgoing payload with the extensions
        await self._process_outgoing_payload(payload, headers)
        # send the payload to the server
        return await self._send_final_payload(payload, headers=headers)

    async def _process_outgoing_payload(self, payload, headers):
        """Process the outgoing *payload* and *headers* with the extensions

        :param list[dict] payload: A list of messages
        :param dict headers: Headers to send
        """
        for extension in self._extensions:
            await extension.outgoing(payload, headers)
        if self._auth:
            await self._auth.outgoing(payload, headers)

    @abstractmethod
    async def _send_final_payload(self, payload, *, headers):
        """Send *payload* to server

        Send the *payload* to the server and return once a
        response message can be provided for the first message in the
        *payload*.
        When reimplementing this method keep in mind that the server will
        likely return additional responses which should be enqueued for
        consumers. To enqueue the received messages :meth:`_consume_payload`
        can be used.

        :param list[dict] payload: A list of messages
        :param dict headers: Headers to send
        :return: The response message for the first message in the *payload*
        :rtype: dict
        :raises TransportError: When the network request fails.
        """

    def _is_matching_response(self, response_message, message):
        """Check whether the *response_message* is a response for the
        given *message*.

        :param dict message: A sent message
        :param response_message: A response message
        :return: True if the *response_message* is a match for *message*
                 otherwise False.
        :rtype: bool
        """
        if message is None or response_message is None:
            return False
        # to consider a response message as a pair of the sent message
        # their channel should match, if they contain an id field it should
        # also match (according to the specs an id is always optional),
        # and the response message should contain the successful field
        return (message["channel"] == response_message["channel"] and
                message.get("id") == response_message.get("id") and
                "successful" in response_message)

    def _is_server_error_message(self, response_message):
        """Check whether the *response_message* is a server side error message

        :param response_message: A response message
        :return: True if the *response_message* is a server side error message
                 otherwise False.
        :rtype: bool
        """
        return (response_message["channel"] not in [MetaChannel.CONNECT,
                                                    MetaChannel.DISCONNECT,
                                                    MetaChannel.HANDSHAKE] and
                not response_message.get("successful", True))

    def _is_event_message(self, response_message):
        """Check whether the *response_message* is an event message

        :param response_message: A response message
        :return: True if the *response_message* is an event message
                 otherwise False.
        :rtype: bool
        """
        channel = response_message["channel"]
        return (not channel.startswith(META_CHANNEL_PREFIX) and
                not channel.startswith(SERVICE_CHANNEL_PREFIX) and
                "data" in response_message)

    def _is_auth_error_message(self, response_message):
        """Check whether the *response_message* is an authentication error
        message

        :param response_message: A response message
        :return: True if the *response_message* is an authentication error \
        message, otherwise False.
        :rtype: bool
        """
        error_code = get_error_code(response_message.get("error"))
        # Strictly speaking, only UNAUTHORIZED should be considered as an auth
        # error, but some channels can also react with FORBIDDEN for auth
        # failures. This is certainly true for /meta/handshake, and since this
        # might happen for other channels as well, it's better to stay safe
        # and treat FORBIDDEN also as a potential auth error
        return error_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)

    def _consume_message(self, response_message):
        """Enqueue the *response_message* for consumers if it's a type of
        message that consumers should receive

        :param response_message: A response message
        """
        if (self._is_server_error_message(response_message) or
                self._is_event_message(response_message)):
            self._enqueue_message(response_message)

    def _update_subscriptions(self, response_message):
        """Update the set of subscriptions based on the *response_message*

       :param response_message: A response message
        """
        # if a subscription response is successful, then add the channel
        # to the set of subscriptions, if it fails, then remove it
        if response_message["channel"] == MetaChannel.SUBSCRIBE:
            if (response_message["successful"] and
                    response_message["subscription"]
                    not in self._subscriptions):
                self._subscriptions.add(response_message["subscription"])
            elif (not response_message["successful"] and
                  response_message["subscription"] in self._subscriptions):
                self._subscriptions.remove(response_message["subscription"])

        # if an unsubscribe response is successful then remove the channel
        # from the set of subscriptions
        if response_message["channel"] == MetaChannel.UNSUBSCRIBE:
            if (response_message["successful"] and
                    response_message["subscription"] in self._subscriptions):
                self._subscriptions.remove(response_message["subscription"])

    async def _process_incoming_payload(self, payload, headers=None):
        """Process incoming *payload* and *headers* with the extensions

        :param payload: A list of response messages
        :type payload: list[dict]
        :param headers: Received headers
        :type headers: dict or None
        """
        if self._auth:
            await self._auth.incoming(payload, headers)
        for extension in self._extensions:
            await extension.incoming(payload, headers)

    async def _consume_payload(self, payload, *, headers=None,
                               find_response_for=None):
        """Enqueue event messages for the consumers and update the internal
        state of the transport, based on response messages in the *payload*.

        :param payload: A list of response messages
        :type payload: list[dict]
        :param headers: Received headers
        :type headers: dict or None
        :param dict find_response_for: Find and return the matching \
        response message for the given *find_response_for* message.
        :return: The response message for the *find_response_for* message, \
        otherwise ``None``
        :rtype: dict or None
        """
        # process incoming payload and headers with the extensions
        await self._process_incoming_payload(payload, headers)

        # return None if no response message is found for *find_response_for*
        result = None
        for message in payload:
            # if there is an advice in the message then update the transport's
            # reconnect advice
            if "advice" in message:
                self._reconnect_advice = message["advice"]

            # update subscriptions based on responses
            self._update_subscriptions(message)

            # set the message as the result and continue if it is a matching
            # response
            if (result is None and
                    self._is_matching_response(message, find_response_for)):
                result = message
                continue

            self._consume_message(message)
        return result

    def _enqueue_message(self, message):
        """Enqueue *message* for consumers or drop the message if the queue \
        is full

        :param message: A response message
        """
        try:
            self.incoming_queue.put_nowait(message)
        except asyncio.QueueFull:
            LOGGER.warning("Incoming message queue is full, "
                           "dropping message: %r", message)

    def _start_connect_task(self, coro):
        """Wrap the *coro* in a future and schedule it

        The future is stored internally in :obj:`_connect_task`. The future's
        results will be consumed by :obj:`_connect_done`.

        :param coro: Coroutine
        :return: Future
        """
        self._connect_task = asyncio.ensure_future(coro, loop=self._loop)
        self._connect_task.add_done_callback(self._connect_done)
        return self._connect_task

    async def _stop_connect_task(self):
        """Stop the connection task

        If no connect task exists or if it's done it does nothing.
        """
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            await asyncio.wait([self._connect_task])

    async def connect(self):
        """Connect to the server

        The transport will try to start and maintain a continuous connection
        with the server, but it'll return with the response of the first
        successful connection as soon as possible.

        :return dict: The response of the first successful connection.
        :raise TransportInvalidOperation: If the transport doesn't has a \
        client id yet, or if it's not in a :obj:`~TransportState.DISCONNECTED`\
        :obj:`state`.
        :raises TransportError: When the network request fails.
        """
        if not self.client_id:
            raise TransportInvalidOperation(
                "Can't connect to the server without a client id. "
                "Do a handshake first.")
        if self.state != TransportState.DISCONNECTED:
            raise TransportInvalidOperation(
                "Can't connect to a server without disconnecting first.")

        self._state = TransportState.CONNECTING
        return await self._start_connect_task(self._connect())

    async def _connect(self, delay=None):
        """Connect to the server

        :param delay: Initial connection delay
        :type delay: None or int or float
        :return: Connect response
        :rtype: dict
        :raises TransportError: When the network request fails.
        """
        if delay:
            await asyncio.sleep(delay, loop=self._loop)
        message = CONNECT_MESSAGE.copy()
        payload = [message]
        if self._subscribe_on_connect and self.subscriptions:
            for subscription in self.subscriptions:
                extra_message = SUBSCRIBE_MESSAGE.copy()
                extra_message["subscription"] = subscription
                payload.append(extra_message)
        result = await self._send_payload_with_auth(payload)
        self._subscribe_on_connect = not result["successful"]
        return result

    def _connect_done(self, future):
        """Consume the result of the *future* and follow the server's \
        connection advice if the transport is still connected

        :param asyncio.Future future: A :obj:`_connect` or :obj:`_handshake` \
        future
        """
        try:
            result = future.result()
            reconnect_timeout = self._reconnect_advice["interval"]
            if self.state == TransportState.CONNECTING:
                self._state = TransportState.CONNECTED
                self._connected_event.set()
                self._connecting_event.clear()
        except Exception as error:  # pylint: disable=broad-except
            result = error
            reconnect_timeout = self._reconnect_timeout
            if self.state == TransportState.CONNECTED:
                self._state = TransportState.CONNECTING
                self._connecting_event.set()
                self._connected_event.clear()

        LOGGER.debug("Connect task finished with: %r", result)

        if self.state != TransportState.DISCONNECTING:
            self._follow_advice(reconnect_timeout)

    def _follow_advice(self, reconnect_timeout):
        """Follow the server's reconnect advice

        Either a :obj:`_connect` or :obj:`_handshake` operation is started
        based on the advice or the method returns without starting any
        operation if no advice is provided.

        :param reconnect_timeout: Initial connection delay to pass to \
        :obj:`_connect` or :obj:`_handshake`.
        """
        advice = self._reconnect_advice.get("reconnect")
        # do a handshake operation if advised
        if advice == "handshake":
            self._start_connect_task(
                self._handshake([self.connection_type],
                                delay=reconnect_timeout)
            )
        # do a connect operation if advised
        elif advice == "retry":
            self._start_connect_task(self._connect(delay=reconnect_timeout))
        # there is not reconnect advice from the server or its value
        # is none
        else:
            LOGGER.warning("No reconnect advice provided, no more operations "
                           "will be scheduled.")
            # if there is a finished connect taks enqueue its result
            # since it's a server error that should be handled by the
            # consumer
            if self._connect_task and self._connect_task.done():
                self._enqueue_message(self._connect_task.result())
            self._state = TransportState.DISCONNECTED

    async def disconnect(self):
        """Disconnect from server

        :raises TransportError: When the network request fails.
        """
        try:
            self._state = TransportState.DISCONNECTING
            await self._stop_connect_task()
            await self._send_message(DISCONNECT_MESSAGE.copy())
        finally:
            self._state = TransportState.DISCONNECTED

    async def close(self):
        """Close transport and release resources"""
        await self._close_http_session()

    async def subscribe(self, channel):
        """Subscribe to *channel*

        :param str channel: Name of the channel
        :return: Subscribe response
        :rtype: dict
        :raise TransportInvalidOperation: If the transport is not in the \
        :obj:`~TransportState.CONNECTED` or :obj:`~TransportState.CONNECTING` \
        :obj:`state`
        :raises TransportError: When the network request fails.
        """
        if self.state not in [TransportState.CONNECTING,
                              TransportState.CONNECTED]:
            raise TransportInvalidOperation(
                "Can't subscribe without being connected to a server.")
        return await self._send_message(SUBSCRIBE_MESSAGE.copy(),
                                        subscription=channel)

    async def unsubscribe(self, channel):
        """Unsubscribe from *channel*

        :param str channel: Name of the channel
        :return: Unsubscribe response
        :rtype: dict
        :raise TransportInvalidOperation: If the transport is not in the \
        :obj:`~TransportState.CONNECTED` or :obj:`~TransportState.CONNECTING` \
        :obj:`state`
        :raises TransportError: When the network request fails.
        """
        if self.state not in [TransportState.CONNECTING,
                              TransportState.CONNECTED]:
            raise TransportInvalidOperation(
                "Can't unsubscribe without being connected to a server.")
        return await self._send_message(UNSUBSCRIBE_MESSAGE.copy(),
                                        subscription=channel)

    async def publish(self, channel, data):
        """Publish *data* to the given *channel*

        :param str channel: Name of the channel
        :param dict data: Data to send to the server
        :return: Publish response
        :rtype: dict
        :raise TransportInvalidOperation: If the transport is not in the \
        :obj:`~TransportState.CONNECTED` or :obj:`~TransportState.CONNECTING` \
        :obj:`state`
        :raises TransportError: When the network request fails.
        """
        if self.state not in [TransportState.CONNECTING,
                              TransportState.CONNECTED]:
            raise TransportInvalidOperation(
                "Can't publish without being connected to a server.")
        return await self._send_message(PUBLISH_MESSAGE.copy(),
                                        channel=channel,
                                        data=data)