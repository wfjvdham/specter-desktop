import logging
import os
import signal
import sys
import time
from os import path
from socket import gethostname
from urllib.parse import urlparse

import click
from OpenSSL import crypto

from ..server import create_app, init_app, setup_debug_logging
from ..specter_error import SpecterError
from ..util.tor import start_hidden_service, stop_hidden_services

logger = logging.getLogger(__name__)


@click.group()
def cli():
    pass


@cli.command()
# options below can help to run it on a remote server,
# but better use nginx
@click.option(
    "--port", help="TCP port to bind specter to"
)  # default - 25441 set to 80 for http, 443 for https
# set to 0.0.0.0 to make it available outside
@click.option(
    "--host",
    default="127.0.0.1",
    help="if you specify --host 0.0.0.0 then specter will be available in your local LAN.",
)
# for https:
@click.option(
    "--cert",
    help="--cert and --key are for specifying and using a self-signed certificate for SSL encryption.",
)
@click.option(
    "--key",
    help="--cert and --key are for specifying and using a self-signed certificate for SSL encryption.",
)
@click.option(
    "--ssl/--no-ssl",
    is_flag=True,
    default=False,
    help="By default SSL encryption will not be used. Use -ssl to create a self-signed certificate for SSL encryption. You can also specify encryption via --cert and --key.",
)
@click.option("--debug/--no-debug", default=None)
@click.option("--filelog/--no-filelog", default=True)
@click.option("--tor", is_flag=True)
@click.option(
    "--hwibridge",
    is_flag=True,
    help="Start the hwi-bridge to use your HWWs with a remote specter.",
)
@click.option(
    "--enforcehwiinitialisation",
    is_flag=True,
    help="calls enumerate() which is known to cause issues with certain usb-devices plugged in at startup.",
)
@click.option(
    "--devstatus-threshold",
    type=click.Choice(["alpha", "beta", "prod"], case_sensitive=False),
    default=None,
    help="Decide which maturity your extensions need to have in order to load them.",
)
@click.option(
    "--specter-data-folder",
    default=None,
    help="Use a custom specter data-folder. By default it is ~/.specter.",
)
@click.option(
    "--config",
    default=None,
    help="A class from the config.py which sets reasonable default values.",
)
def server(
    port,
    host,
    cert,
    key,
    ssl,
    debug,
    filelog,
    tor,
    hwibridge,
    enforcehwiinitialisation,
    devstatus_threshold,
    specter_data_folder,
    config,
):
    """This code is a function that runs Specter Desktop as a http(s)-service.
    It sets up logging, creates an app to get Specter instance and its data folder,
    sets certificates, initializes the app with the given parameters,
    runs the app with the given parameters,
    and stops any hidden services when it's done.
    """
    # logging
    if debug:
        setup_debug_logging()

    # create an app to get Specter instance
    # and it's data folder
    if config is None:
        app = create_app()
    else:
        if "." in config:
            app = create_app(config=config)
        else:
            app = create_app(config="cryptoadvance.specter.config." + config)

    if specter_data_folder:
        app.config["SPECTER_DATA_FOLDER"] = specter_data_folder

    if port:
        app.config["PORT"] = int(port)

    # devstatus_threshold
    if devstatus_threshold is not None:
        app.config["SERVICES_DEVSTATUS_THRESHOLD"] = devstatus_threshold

    # certificates
    if cert:
        logger.info("CERT:" + str(cert))
        app.config["CERT"] = cert
    if key:
        app.config["KEY"] = key

    # the app.config needs to be configured before init_app, such that the service callbacks
    # like after_serverpy_init_app have this information available
    if host != app.config["HOST"]:
        app.config["HOST"] = host

    # set up kwargs dict for app.run
    kwargs = {
        "host": host,
        "port": app.config["PORT"],
    }
    # watch templates folder to reload when something changes
    extra_dirs = ["templates"]
    extra_files = extra_dirs[:]
    for extra_dir in extra_dirs:
        for dirname, dirs, files in os.walk(extra_dir):
            for filename in files:
                filename = os.path.join(dirname, filename)
                if os.path.isfile(filename):
                    extra_files.append(filename)
    kwargs["extra_files"] = extra_files

    kwargs = configure_ssl(kwargs, app.config, ssl)

    app.app_context().push()
    if enforcehwiinitialisation:
        app.config["ENFORCE_HWI_INITIALISATION_AT_STARTUP"] = True
    init_app(app, hwibridge=hwibridge)

    if filelog:
        # again logging: Creating a logfile in SPECTER_DATA_FOLDER (which needs to exist)
        app.config["SPECTER_LOGFILE"] = os.path.join(
            app.specter.data_folder, "specter.log"
        )
        fh = logging.FileHandler(app.config["SPECTER_LOGFILE"])
        formatter = logging.Formatter(app.config["SPECTER_LOGFORMAT"])
        fh.setFormatter(formatter)
        logging.getLogger().addHandler(fh)

    toraddr_file = path.join(app.specter.data_folder, "onion.txt")

    if hwibridge:
        if kwargs.get("ssl_context"):
            logger.error(
                "Running the hwibridge is not supported via SSL. Remove --ssl, --cert, and --key options."
            )
            exit(1)
        print(
            " * Running in HWI Bridge mode.\n"
            " * You can configure access to the API "
            "at: %s://%s:%d/hwi/settings" % ("http", host, app.config["PORT"])
        )

    # debug is false by default
    def run(debug=debug):
        try:
            # if we have certificates
            if "ssl_context" in kwargs:
                tor_port = 443
            else:
                tor_port = 80
            app.port = kwargs["port"]
            app.tor_port = tor_port
            app.save_tor_address_to = toraddr_file
            if debug and (tor or os.getenv("CONNECT_TOR") == "True"):
                print(
                    " * Warning: Cannot use Tor in debug mode. \
                      Starting in production mode instead."
                )
                debug = False
            if (
                tor
                or os.getenv("CONNECT_TOR") == "True"
                or app.specter.config["tor_status"] == True
            ):
                try:
                    app.tor_enabled = True
                    start_hidden_service(app)
                    if app.specter.config["tor_status"] == False:
                        app.specter.toggle_tor_status()
                except Exception as e:
                    logger.error(f" * Failed to start Tor hidden service: {e}")
                    logger.error(" * Continuing process with Tor disabled")
                    logger.exception(e)
                    app.tor_service_id = None
                    app.tor_enabled = False
            else:
                app.tor_service_id = None
                app.tor_enabled = False
            app.run(debug=debug, **kwargs)
            stop_hidden_services(app)
        finally:
            try:
                if app.specter.tor_controller is not None:
                    app.specter.tor_controller.close()
            except SpecterError as se:
                # no reason to break startup here
                logger.error("Could not initialize tor-system")

    # if not a daemon we can use DEBUG
    if debug is None:
        debug = app.config["DEBUG"]
    run(debug=debug)


def configure_ssl(kwargs, app_config, ssl):
    """accepts kwargs and adjust them based on the config and ssl"""
    # If we should create a cert but it's not specified where, let's specify the location

    if not ssl and app_config["CERT"] is None:
        return kwargs

    if app_config["CERT"] is None:
        app_config["CERT"] = app_config["SPECTER_DATA_FOLDER"] + "/cert.pem"
    if app_config["KEY"] is None:
        app_config["KEY"] = app_config["SPECTER_DATA_FOLDER"] + "/key.pem"

    if not os.path.exists(app_config["CERT"]):
        logger.info("Creating SSL-cert " + app_config["CERT"])
        # create a key pair
        k = crypto.PKey()
        k.generate_key(crypto.TYPE_RSA, 2048)

        # create a self-signed cert
        cert = crypto.X509()
        cert.get_subject().C = app_config["SPECTER_SSL_CERT_SUBJECT_C"]
        cert.get_subject().ST = app_config["SPECTER_SSL_CERT_SUBJECT_ST"]
        cert.get_subject().L = app_config["SPECTER_SSL_CERT_SUBJECT_L"]
        cert.get_subject().O = app_config["SPECTER_SSL_CERT_SUBJECT_O"]
        cert.get_subject().OU = app_config["SPECTER_SSL_CERT_SUBJECT_OU"]
        cert.get_subject().CN = app_config["SPECTER_SSL_CERT_SUBJECT_CN"]
        cert.set_serial_number(app_config["SPECTER_SSL_CERT_SERIAL_NUMBER"])
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(10 * 365 * 24 * 60 * 60)
        cert.set_issuer(cert.get_subject())
        cert.set_pubkey(k)
        cert.sign(k, "sha1")

        open(app_config["CERT"], "wt").write(
            crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode("utf-8")
        )
        open(app_config["KEY"], "wt").write(
            crypto.dump_privatekey(crypto.FILETYPE_PEM, k).decode("utf-8")
        )

    logger.info("Configuring SSL-certificate " + app_config["CERT"])
    kwargs["ssl_context"] = (app_config["CERT"], app_config["KEY"])
    return kwargs
