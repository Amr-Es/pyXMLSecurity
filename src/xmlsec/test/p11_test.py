"""
Testing the PKCS#11 shim layer
"""

__author__ = 'leifj'

import pkg_resources
import unittest
import logging
import os
import traceback
import subprocess
import shutil
import tempfile

from lxml import etree

import xmlsec
from xmlsec.test import paths_for_component
from xmlsec.test import find_alts
from xmlsec.test import run_cmd

from xmlsec.test.case import load_test_data
from xmlsec.exceptions import XMLSigException

try:
    from PyKCS11 import PyKCS11Error
    from PyKCS11.LowLevel import CKR_PIN_INCORRECT
except ImportError:
    raise unittest.SkipTest("PyKCS11 not installed")

try:
    from xmlsec import pk11
except (ImportError, XMLSigException):
    raise unittest.SkipTest("PyKCS11 not installed")


component_default_paths = {
    'P11_MODULE': [
        '/usr/lib/softhsm/libsofthsm2.so',
        '/usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so',
        '/usr/lib/softhsm/libsofthsm.so',
    ],
    'P11_ENGINE': [
        '/usr/lib/ssl/engines/libpkcs11.so',
        '/usr/lib/engines/engine_pkcs11.so',
        '/usr/lib/x86_64-linux-gnu/engines-1.1/pkcs11.so',
    ],
    'PKCS11_TOOL': [
        '/usr/bin/pkcs11-tool',
    ],
    'OPENSC_TOOL': [
        '/usr/bin/opensc-tool',
    ],
    'SOFTHSM': [
        '/usr/bin/softhsm2-util',
        '/usr/bin/softhsm',
    ],
    'OPENSSL': [
        '/usr/bin/openssl',
    ],
}

component_path = {
    component: find_alts(
        paths_for_component(component, component_default_paths[component])
    )
    for component in component_default_paths.keys()
}

if any(path is None for component, path in component_path.items()):
    missing = [
        component
        for component, path in component_path.items()
        if path is None
    ]
    raise unittest.SkipTest("Required components missing: {}".format(missing))

softhsm_version = 1
if component_path['SOFTHSM'].endswith('softhsm2-util'):
    softhsm_version = 2

openssl_version = subprocess.check_output([component_path['OPENSSL'],
                                          'version']
                                          )[8:11]

p11_test_files = []
softhsm_conf = None
server_cert_pem = None
server_cert_der = None
softhsm_db = None


def _tf():
    f = tempfile.NamedTemporaryFile(delete=False)
    p11_test_files.append(f.name)
    return f.name


def _td():
    d = tempfile.mkdtemp()
    p11_test_files.append(d)
    return d


@unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
def setup():
    logging.debug("Creating test pkcs11 token using softhsm")
    try:
        from xmlsec import pk11 as pk11
    except ImportError:
        raise unittest.SkipTest("PKCS11 tests disabled: unable to import xmlsec.pk11")

    try:
        global softhsm_conf
        softhsm_conf = _tf()
        logging.debug("Generating softhsm.conf")
        with open(softhsm_conf, "w") as f:
            if softhsm_version == 2:
                softhsm_db = _td()
                f.write("""
# Generated by pyXMLSecurity test
directories.tokendir = %s
objectstore.backend = file
log.level = DEBUG
""" % softhsm_db)
            else:
                softhsm_db = _tf()
                f.write("""
# Generated by pyXMLSecurity test
0:%s
""" % softhsm_db)

        logging.debug("Initializing the token")
        run_cmd([component_path['SOFTHSM'],
                 '--slot', '0',
                 '--label', 'test',
                 '--init-token',
                 '--pin', 'secret1',
                 '--so-pin', 'secret2'], softhsm_conf=softhsm_conf)

        logging.debug("Generating 1024 bit RSA key in token")
        run_cmd([component_path['PKCS11_TOOL'],
                 '--module', component_path['P11_MODULE'],
                 '-l',
                 '-k',
                 '--key-type', 'rsa:1024',
                 '--slot', '0',
                 '--id', 'a1b2',
                 '--label', 'test',
                 '--pin', 'secret1'], softhsm_conf=softhsm_conf)
        run_cmd([component_path['PKCS11_TOOL'],
                 '--module', component_path['P11_MODULE'],
                 '-l',
                 '--pin', 'secret1', '-O'], softhsm_conf=softhsm_conf)
        global signer_cert_der
        global signer_cert_pem
        signer_cert_pem = _tf()
        openssl_conf = _tf()
        logging.debug("Generating OpenSSL config for version {}".format(openssl_version))
        with open(openssl_conf, "w") as f:
            dynamic_path = (
                "dynamic_path = %s" % component_path['P11_ENGINE']
                if openssl_version.startswith(b'1.')
                else ""
            )
            f.write("\n".join([
                "openssl_conf = openssl_def",
                "[openssl_def]",
                "engines = engine_section",
                "[engine_section]",
                "pkcs11 = pkcs11_section",
                "[req]",
                "distinguished_name = req_distinguished_name",
                "[req_distinguished_name]",
                "[pkcs11_section]",
                "engine_id = pkcs11",
                dynamic_path,
                "MODULE_PATH = %s" % component_path['P11_MODULE'],
                "PIN = secret1",
                "init = 0",
            ]))

        with open(openssl_conf, "r") as f:
            logging.debug('-------- START DEBUG openssl_conf --------')
            logging.debug(f.readlines())
            logging.debug('-------- END DEBUG openssl_conf --------')
        logging.debug('-------- START DEBUG paths --------')
        logging.debug(run_cmd(['ls', '-ld', component_path['P11_ENGINE']]))
        logging.debug(run_cmd(['ls', '-ld', component_path['P11_MODULE']]))
        logging.debug('-------- END DEBUG paths --------')

        signer_cert_der = _tf()

        logging.debug("Generating self-signed certificate")
        run_cmd([component_path['OPENSSL'], 'req',
                 '-new',
                 '-x509',
                 '-subj', "/CN=Test Signer",
                 '-engine', 'pkcs11',
                 '-config', openssl_conf,
                 '-keyform', 'engine',
                 '-key', 'label_test',
                 '-passin', 'pass:secret1',
                 '-out', signer_cert_pem], softhsm_conf=softhsm_conf)

        run_cmd([component_path['OPENSSL'], 'x509',
                 '-inform', 'PEM',
                 '-outform', 'DER',
                 '-in', signer_cert_pem,
                 '-out', signer_cert_der], softhsm_conf=softhsm_conf)

        logging.debug("Importing certificate into token")

        run_cmd([component_path['PKCS11_TOOL'],
                 '--module', component_path['P11_MODULE'],
                 '-l',
                 '--slot-index', '0',
                 '--id', 'a1b2',
                 '--label', 'test',
                 '-y', 'cert',
                 '-w', signer_cert_der,
                 '--pin', 'secret1'], softhsm_conf=softhsm_conf)

    except Exception as ex:
        print("-" * 64)
        traceback.print_exc()
        print("-" * 64)
        logging.error("PKCS11 tests disabled: unable to initialize test token: %s" % ex)
        raise ex


def teardown(self):
    for o in self.p11_test_files:
        if os.path.exists(o):
            if os.path.isdir(o):
                shutil.rmtree(o)
            else:
                os.unlink(o)
    self.p11_test_files = []


class TestPKCS11(unittest.TestCase):
    def setUp(self):
        datadir = pkg_resources.resource_filename(__name__, 'data')
        self.private_keyspec = os.path.join(datadir, 'test.key')
        self.public_keyspec = os.path.join(datadir, 'test.pem')

        self.cases = load_test_data('data/signverify')

    @unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
    def test_open_session(self):
        session = None
        try:
            os.environ['SOFTHSM_CONF'] = softhsm_conf
            os.environ['SOFTHSM2_CONF'] = softhsm_conf
            session = pk11._session(component_path['P11_MODULE'], pk11_uri="pkcs11://%s/test?pin=secret1" % component_path['P11_MODULE'])
            assert session is not None
        except Exception as ex:
            traceback.print_exc()
            raise ex
        finally:
            if session is not None:
                pk11._close_session(session)

    @unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
    def test_open_session_no_pin(self):
        session = None
        try:
            os.environ['SOFTHSM_CONF'] = softhsm_conf
            os.environ['SOFTHSM2_CONF'] = softhsm_conf
            session = pk11._session(component_path['P11_MODULE'], pk11_uri="pkcs11://%s/test" % component_path['P11_MODULE'])
            assert session is not None
        except Exception as ex:
            traceback.print_exc()
            raise ex
        finally:
            if session is not None:
                pk11._close_session(session)

    # @unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
    @unittest.skip("SoftHSM PKCS11 module does not support 2 sessions")
    def test_two_sessions(self):
        session1 = None
        session2 = None
        try:
            os.environ['SOFTHSM_CONF'] = softhsm_conf
            os.environ['SOFTHSM2_CONF'] = softhsm_conf
            session1 = pk11._session(component_path['P11_MODULE'], pk11_uri="pkcs11://%s/test?pin=secret1" % component_path['P11_MODULE'])
            session2 = pk11._session(component_path['P11_MODULE'], pk11_uri="pkcs11://%s/test?pin=secret1" % component_path['P11_MODULE'])
            assert session1 != session2
            assert session1 is not None
            assert session2 is not None
        except Exception as ex:
            raise ex
        finally:
            if session1 is not None:
                pk11._close_session(session1)
            if session2 is not None:
                pk11._close_session(session2)

    @unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
    def test_bad_login(self):
        os.environ['SOFTHSM_CONF'] = softhsm_conf
        os.environ['SOFTHSM2_CONF'] = softhsm_conf
        try:
            session = pk11._session(component_path['P11_MODULE'], pk11_uri="pkcs11://%s/test?pin=wrong" % component_path['P11_MODULE'])
            assert False, "We should have failed the last login"
        except PyKCS11Error as ex:
            assert ex.value == CKR_PIN_INCORRECT
            pass

    @unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
    def test_find_key(self):
        session = None
        try:
            os.environ['SOFTHSM_CONF'] = softhsm_conf
            os.environ['SOFTHSM2_CONF'] = softhsm_conf
            session = pk11._session(component_path['P11_MODULE'], pk11_uri="pkcs11://%s/test?pin=secret1" % component_path['P11_MODULE'])
            key, cert = pk11._find_key(session, "test")
            assert key is not None
            assert cert is not None
            assert cert.strip() == open(signer_cert_pem).read().strip().encode('utf-8')
        except Exception as ex:
            raise ex
        finally:
            if session is not None:
                pk11._close_session(session)

    @unittest.skipIf(component_path['P11_MODULE'] is None, "SoftHSM PKCS11 module not installed")
    def test_SAML_sign_with_pkcs11(self):
        """
        Test signing a SAML assertion using PKCS#11 and then verifying it using plain file.
        """
        case = self.cases['SAML_assertion1']
        print("XML input :\n{}\n\n".format(case.as_buf('in.xml')))

        os.environ['SOFTHSM_CONF'] = softhsm_conf
        os.environ['SOFTHSM2_CONF'] = softhsm_conf

        signed = xmlsec.sign(case.as_etree('in.xml'),
                             key_spec="pkcs11://%s/test?pin=secret1" % component_path['P11_MODULE'])

        # verify signature using the public key
        res = xmlsec.verify(signed, signer_cert_pem)
        self.assertTrue(res)

    def test_SAML_sign_with_pkcs11_cert(self):
        """
        Test signing a SAML assertion using PKCS#11 and then verifying it using plain file.
        """
        case = self.cases['SAML_assertion1']
        print("XML input :\n{}\n\n".format(case.as_buf('in2.xml')))

        os.environ['SOFTHSM_CONF'] = softhsm_conf
        os.environ['SOFTHSM2_CONF'] = softhsm_conf

        signed = xmlsec.sign(case.as_etree('in2.xml'),
                             key_spec="pkcs11://%s/test?pin=secret1" % component_path['P11_MODULE'])

        print("XML output :\n{}\n\n".format(etree.tostring(signed)))
        # verify signature using the public key
        res = xmlsec.verify(signed, signer_cert_pem)
        self.assertTrue(res)
