#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2024 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import asyncio
import datetime
import os
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from time import time, sleep

try:
    import aioquic
    HAVE_AIOQUIC = bool(aioquic)
except ImportError:
    HAVE_AIOQUIC = False
def _make_key_and_cert(common_name: str, not_after_days: int = 30, signing_key=None):
    """Return (key_pem, cert_pem) for a throwaway self-signed certificate.

    Pass signing_key= to sign with an existing key (used for the
    same-key-new-cert renewal case)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = signing_key or ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=not_after_days))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


class ImmediateLoop:
    """Stand-in for an asyncio loop that runs call_soon_threadsafe inline."""

    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append(callback)
        callback(*args)


class DeadLoop:
    def call_soon_threadsafe(self, callback, *args):
        raise RuntimeError("event loop is closed")


class SleepingLoop:
    """Never runs callbacks on its own; stores them so tests can flush later
    and prove a timed-out swap still applies."""

    def __init__(self):
        self.pending = []

    def call_soon_threadsafe(self, callback, *args):
        self.pending.append((callback, args))


@unittest.skipUnless(HAVE_AIOQUIC, "aioquic not available")
class TestValidateCertificateFiles(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.key_a_pem, self.cert_a_pem = _make_key_and_cert("validate-a")
        self.key_b_pem, self.cert_b_pem = _make_key_and_cert("validate-b")
        self.cert_path = os.path.join(self.tmpdir.name, "cert.pem")
        self.key_path = os.path.join(self.tmpdir.name, "key.pem")

    def _write(self, cert_data, key_data):
        for path, data in ((self.cert_path, cert_data), (self.key_path, key_data)):
            with open(path, "wb") as f:
                f.write(data)

    def _validate(self):
        from xpra.net.quic.listener import validate_certificate_files
        return validate_certificate_files(self.cert_path, self.key_path)

    def test_good_files_validate(self):
        self._write(self.cert_a_pem, self.key_a_pem)
        certificate, chain, private_key = self._validate()
        assert chain == []
        assert private_key is not None
        assert certificate.serial_number is not None

    def test_validate_same_key_new_cert(self):
        # the acme.sh renewal case: new cert signed by the same private key
        from cryptography.hazmat.primitives import serialization
        key_a = serialization.load_pem_private_key(self.key_a_pem, password=None)
        _, cert_b_pem = _make_key_and_cert("validate-a-renewed", signing_key=key_a)
        self._write(cert_b_pem, self.key_a_pem)
        certificate, _, _ = self._validate()
        assert certificate.serial_number is not None

    def test_validate_corrupt_cert(self):
        self._write(b"this is not a certificate", self.key_a_pem)
        with self.assertRaises(ValueError) as raised:
            self._validate()
        assert self.cert_path in str(raised.exception)

    def test_validate_missing_file(self):
        with self.assertRaises(ValueError) as raised:
            self._validate()
        assert self.cert_path in str(raised.exception)

    def test_validate_key_mismatch(self):
        # cert B on disk, key A on disk: the out-of-sync renewal failure mode
        self._write(self.cert_b_pem, self.key_a_pem)
        with self.assertRaises(ValueError) as raised:
            self._validate()
        assert "does not match" in str(raised.exception)

    def test_validate_corrupt_key(self):
        # valid cert with a garbage key file pins the validate-first order:
        # aioquic assigns the certificate before reading the key, so loading
        # straight into the live configuration would corrupt it right here
        self._write(self.cert_b_pem, b"not a private key")
        with self.assertRaises(ValueError) as raised:
            self._validate()
        assert self.key_path in str(raised.exception)


@unittest.skipUnless(HAVE_AIOQUIC, "aioquic not available")
class TestApplyQuicCertificate(unittest.TestCase):

    def setUp(self):
        from aioquic.quic.configuration import QuicConfiguration
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        key_pem, cert_pem = _make_key_and_cert("apply-a")
        self.cert_path = os.path.join(self.tmpdir.name, "cert.pem")
        self.key_path = os.path.join(self.tmpdir.name, "key.pem")
        for path, data in ((self.cert_path, cert_pem), (self.key_path, key_pem)):
            with open(path, "wb") as f:
                f.write(data)
        self.configuration = QuicConfiguration(is_client=False)
        self.configuration.load_cert_chain(self.cert_path, self.key_path)
        self.old_serial = self.configuration.certificate.serial_number
        self.key_a_pem = key_pem
        self.new_certificate, self.new_chain, self.new_private_key = \
            self._write_b_and_validate()
        self.ticket_store = self._make_store_with_ticket()

    def _validated_tuple(self):
        from xpra.net.quic.listener import validate_certificate_files
        return validate_certificate_files(self.cert_path, self.key_path)

    def _make_store_with_ticket(self):
        from xpra.net.quic.session_ticket_store import SessionTicketStore
        from aioquic.tls import SessionTicket, CipherSuite
        store = SessionTicketStore()
        now = datetime.datetime.now(datetime.timezone.utc)
        store.add(SessionTicket(
            age_add=0,
            cipher_suite=CipherSuite.AES_128_GCM_SHA256,
            not_valid_before=now,
            not_valid_after=now + datetime.timedelta(days=1),
            resumption_secret=b"x" * 32,
            server_name="localhost",
            ticket=b"old-ticket",
        ))
        return store

    def _write_b_and_validate(self):
        """Write a second cert/key pair over the paths, then validate it.

        setUp already loaded the live configuration from cert A, so the
        validated tuple must come from cert B for the swap to be observable."""
        key_b_pem, cert_b_pem = _make_key_and_cert("apply-b")
        for path, data in ((self.cert_path, cert_b_pem), (self.key_path, key_b_pem)):
            with open(path, "wb") as f:
                f.write(data)
        return self._validated_tuple()

    def _apply(self, loop, timeout=10):
        return self._apply_with(
            self.new_certificate, self.new_chain, self.new_private_key,
            loop, timeout=timeout)

    def test_apply_swaps_and_clears_ticket_store(self):
        summary = self._apply(ImmediateLoop())
        assert self.configuration.certificate.serial_number != self.old_serial
        assert self.configuration.certificate_chain == self.new_chain
        assert self.configuration.private_key == self.new_private_key
        assert self.ticket_store.tickets == {}
        assert "notAfter" in summary

    def test_apply_dead_loop_raises(self):
        with self.assertRaises(ValueError):
            self._apply(DeadLoop())
        # nothing was swapped:
        assert self.configuration.certificate.serial_number == self.old_serial

    def test_apply_timeout_reports_failure(self):
        loop = SleepingLoop()
        with self.assertRaises(ValueError) as raised:
            self._apply(loop, timeout=0.2)
        assert "may still be applied" in str(raised.exception)
        assert self.configuration.certificate.serial_number == self.old_serial
        # the queued swap of already-validated material still applies once
        # the loop runs it — specified behavior, disclosed in the error:
        for callback, args in loop.pending:
            callback(*args)
        assert self.configuration.certificate.serial_number != self.old_serial
        assert self.ticket_store.tickets == {}

    def test_apply_same_key_renewal(self):
        # the acme.sh case at the apply level: new cert signed by the same key
        from cryptography.hazmat.primitives import serialization
        key_a = serialization.load_pem_private_key(self.key_a_pem, password=None)
        _, cert_c_pem = _make_key_and_cert("apply-a-renewed", signing_key=key_a)
        # setUp overwrote the key file with key B; put key A back so cert C
        # (signed by key A) validates against it
        with open(self.cert_path, "wb") as f:
            f.write(cert_c_pem)
        with open(self.key_path, "wb") as f:
            f.write(self.key_a_pem)
        certificate, chain, private_key = self._validated_tuple()
        summary = self._apply_with(certificate, chain, private_key, ImmediateLoop())
        assert self.configuration.certificate.serial_number != self.old_serial
        assert "notAfter" in summary

    def _apply_with(self, certificate, chain, private_key, loop, timeout=10):
        from xpra.net.quic.listener import apply_quic_certificate
        return apply_quic_certificate(
            self.configuration,
            certificate, chain, private_key,
            self.ticket_store, loop, self.cert_path, timeout=timeout,
        )


@unittest.skipUnless(HAVE_AIOQUIC, "aioquic not available")
class TestListenQuicRegistration(unittest.TestCase):

    def _cert_files(self, name):
        key_pem, cert_pem = _make_key_and_cert(name)
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        cert_path = os.path.join(tmpdir.name, "cert.pem")
        key_path = os.path.join(tmpdir.name, "key.pem")
        for path, data in ((cert_path, cert_pem), (key_path, key_pem)):
            with open(path, "wb") as f:
                f.write(data)
        return cert_path, key_path

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time() + timeout
        while time() < deadline:
            if predicate():
                return True
            sleep(0.05)
        return False

    def test_listen_registers_and_cleanup_unregisters(self):
        from xpra.net.quic.listener import listen_quic
        cert_path, key_path = self._cert_files("listen-registration")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        server = MagicMock()
        server.get_ssl_socket_options = lambda opts: {"cert": cert_path, "key": key_path}
        cleanup = listen_quic(sock, server, {})
        self.addCleanup(cleanup)
        assert self._wait_for(lambda: server.add_quic_configuration.called), \
            "add_quic_configuration was not called"
        cert_arg, key_arg, configuration, ticket_store, loop = \
            server.add_quic_configuration.call_args[0]
        assert cert_arg == cert_path
        assert key_arg == key_path
        from aioquic.quic.configuration import QuicConfiguration
        from xpra.net.quic.session_ticket_store import SessionTicketStore
        assert isinstance(configuration, QuicConfiguration)
        assert isinstance(ticket_store, SessionTicketStore)
        assert loop is not None
        cleanup()
        assert self._wait_for(lambda: server.remove_quic_configuration.called), \
            "remove_quic_configuration was not called"
        server.remove_quic_configuration.assert_called_once_with(configuration)

    def test_cleanup_before_start_never_registers(self):
        import threading
        from xpra.net.quic import listener as quic_listener
        from xpra.net.quic.listener import listen_quic
        cert_path, key_path = self._cert_files("listen-race")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        server = MagicMock()
        server.get_ssl_socket_options = lambda opts: {"cert": cert_path, "key": key_path}
        # track when the listener's do_listen completes, so the test fails
        # (not passes vacuously) if startup never settles on the threaded loop
        real_do_listen = quic_listener.do_listen
        settled = threading.Event()

        async def tracked_do_listen(*args, **kwargs):
            result = await real_do_listen(*args, **kwargs)
            # signal only after the whole listener task step has run: a
            # call_soon callback fires after start_listener's post-await
            # continuation (endpoint assignment, closing check, register or
            # close_endpoint) completes, so the assert cannot race it
            asyncio.get_running_loop().call_soon(settled.set)
            return result

        # the patch must stay active while start_listener runs on the
        # threaded loop, so it covers the whole body:
        with patch("xpra.net.quic.listener.do_listen", tracked_do_listen):
            cleanup = listen_quic(sock, server, {})
            self.addCleanup(cleanup)
            # invoke cleanup now, before startup settles: the registration check
            # runs after the loop has settled either way, so a closing flag set
            # before start_listener's registration step must leave nothing
            # registered (and nothing to unregister)
            cleanup()
            # whichever order the loop races — close_endpoint before
            # start_listener's first step (nothing registered, nothing removed)
            # or after do_listen completed (registered, then unregistered) — a
            # registered entry must never outlive the cleanup:
            assert settled.wait(timeout=5), "listener startup never settled"
            if server.add_quic_configuration.called:
                # registration won the race: the queued close_endpoint must
                # match it with an unregistration before we assert
                assert self._wait_for(lambda: server.remove_quic_configuration.called), \
                    "registered listener was never unregistered"
            assert server.add_quic_configuration.called == \
                server.remove_quic_configuration.called


def main():
    unittest.main()


if __name__ == "__main__":
    main()
