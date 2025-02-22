# pylint: disable=E0203
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
from twisted.internet.protocol import ClientFactory
from twisted.internet import defer, reactor
from txamqp.client import TwistedDelegate
from jasmin.queues.protocol import AmqpProtocol

LOG_CATEGORY = "jasmin-amqp-factory"


class AmqpFactory(ClientFactory):
    protocol = AmqpProtocol

    def __init__(self, config):
        self.reconnectTimer = None
        self.connectionRetry = True
        self.connected = False
        self.config = config
        self.channelReady = None

        self.delegate = TwistedDelegate()

        self.amqp = None  # The protocol instance.
        self.client = None  # Alias for protocol instance

        self.queues = []

        # Set up a dedicated logger
        self.log = logging.getLogger(LOG_CATEGORY)
        if len(self.log.handlers) != 1:
            self.log.setLevel(self.config.log_level)
            if 'stdout' in self.config.log_file:
                handler = logging.StreamHandler(sys.stdout)
            else:
                handler = TimedRotatingFileHandler(filename=self.config.log_file,
                                                   when=self.config.log_rotate)
            formatter = logging.Formatter(self.config.log_format, self.config.log_date_format)
            handler.setFormatter(formatter)
            self.log.addHandler(handler)
            self.log.propagate = False

    def preConnect(self):
        """Initiate deferreds before connecting
        these deferreds are initiated separately and not within self._connect()
        because this one is not called when jasmin is ran as a twistd plugin.
        """

        self.connectionRetry = True

        self.exitDeferred = defer.Deferred()
        if self.channelReady is None:
            self.channelReady = defer.Deferred()

        try:
            # Check if connectDeferred is already set
            self.connectDeferred

            # Reset deferred if it were called before
            if self.connectDeferred.called is True:
                self.connectDeferred = defer.Deferred()
                self.connectDeferred.addCallback(self.authenticate)
        except AttributeError:
            # Set connectDeferred
            self.connectDeferred = defer.Deferred()
            self.connectDeferred.addCallback(self.authenticate)

    def startedConnecting(self, connector):
        self.log.info("Connecting to %s ...", connector.getDestination())

    def getExitDeferred(self):
        """Get a Deferred so you can be notified on disconnect and exited
        This deferred is called once disconnection occurs without a further
        reconnection retrys
        """
        return self.exitDeferred

    def getChannelReadyDeferred(self):
        """Get a Deferred so you can be notified when channel is ready
        """
        return self.channelReady

    MAX_RETRIES = 5

    def clientConnectionFailed(self, connector, reason):
        self.log.error("Connection failed. Reason: %s", str(reason))
        self.connected = False

        if self.reconnectTimer and self.reconnectTimer.active():
            return

        # Increment retry counter
        self.retries += 1

        if self.retries >= self.MAX_RETRIES:
            self.log.error("Reached maximum reconnect attempts, aborting.")
            self.connectDeferred.errback(reason)
            self.exitDeferred.callback(self)
            return

        # Calculate delay with exponential backoff
        delay = 2**self.retries * self.config.reconnectOnConnectionFailureDelay

        self.reconnectTimer = reactor.callLater(delay, self.reConnect, connector)

    def clientConnectionLost(self, connector, reason):
        if not 'Connection was closed cleanly.' in str(reason):
            # Log and attempt reconnect for unexpected losses
            self.log.error("Connection lost. Reason: %s", str(reason))
            self.connected = False
            self.reConnect(connector)
        else:
            # Graceful disconnect, don't retry
            self.connected = False
            self.exitDeferred.callback(self)
            self.log.info("Exiting.")

    def reConnect(self, connector=None):
        if connector is None:
            self.log.error("No connector to retry !")
        else:
            # And try to connect again
            self.preConnect()
            connector.connect()

    def _connect(self):
        self.log.info('Establishing TCP connection to %s:%d', self.config.host, self.config.port)
        connect_deferred = defer.Deferred()
        reactor.connectTCP(self.config.host, self.config.port, self)
        connect_deferred.callback(lambda _: self.preConnect())
        return connect_deferred

    def connect(self):
        self._connect()

        return self.connectDeferred

    def buildProtocol(self, addr):
        # If heartbeat is 0, it is disabled, otherwise heartbeat is the number
        # of seconds between each AMQP heartbeat. Defaults to 0
        p = self.protocol(self.delegate, self.config.vhost, self.config.getSpec(),
                          heartbeat=self.config.heartbeat)
        p.factory = self  # Tell the protocol about this factory.

        self.client = p  # Store the protocol.

        return p

    def authenticate(self, ignore):
        # Authenticate.
        deferred = self.client.start({"LOGIN": self.config.username, "PASSWORD": self.config.password})
        deferred.addCallback(self._authenticated)
        deferred.addErrback(self._authentication_failed)

    def _authenticated(self, ignore):
        """Called when the connection has been authenticated."""
        self.log.info("Successfull authentication")

        # Get a channel.
        d = self.client.channel(1)
        d.addCallback(self._got_channel)
        d.addErrback(self._got_channel_failed)

    def _got_channel(self, chan):
        self.log.info("Got channel")

        self.chan = chan
        self.queues = []

        d = self.chan.channel_open()
        d.addCallback(self._channel_open)
        d.addErrback(self._channel_open_failed)

    def _channel_open(self, arg):
        """Called when the channel is open."""
        self.log.info("The channel is open")

        # Flag that the connection is open.
        self.connected = True
        self.channelReady.callback(self)

    def _channel_open_failed(self, error):
        self.log.error("Channel open failed: %s", error)

    def _got_channel_failed(self, error):
        self.log.error("Error getting channel: %s", error)

    def _authentication_failed(self, error):
        self.log.error("AMQP authentication failed: %s", error)

    def disconnect(self, reason=None):
        self.channelReady = False

        if self.client is not None:
            return self.client.close(reason)

        return None

    def named_queue_declare(self, *args, **keys):
        """This is a wrapper to channel's queue_declare method
        it is intended to avoid multiple declaration of the same queue
        using self.queues which holds all declared queues in the connection
        """

        if not self.connected:
            self.log.error("AMQP Client is not connected, cannot queue_declare")
            return None

        for q in self.queues:
            if q == keys['queue']:
                self.log.debug('Queue [%s] is already declared, its okay .. no need to redeclare it', q)
                return None

        return self.chan.queue_declare(*args, **keys).addCallback(self._queue_declared)

    def _queue_declared(self, queue):
        self.log.info("A new queue has been successfully declared [%s]", queue.queue)
        self.queues.append(queue.queue)

    def publish(self, **args):
        """This is a wrapper to channel's publish method
        it is intended for connection checking before publishing
        """

        if not self.connected:
            self.log.error("AMQP Client is not connected, cannot publish: %s", args)
            return None

        return self.chan.basic_publish(**args)

    def stopConnectionRetrying(self):
        """This will stop the factory from reconnecting
        It is used whenever a service stop has been requested, the connectionRetry flag
        is reset to True upon connect() call
        """

        if self.reconnectTimer and self.reconnectTimer.active():
            self.reconnectTimer.cancel()
            self.reconnectTimer = None

        self.connectionRetry = False

    def disconnectAndDontRetryToConnect(self):
        self.stopConnectionRetrying()
        return self.disconnect()
