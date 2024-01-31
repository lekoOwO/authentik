"""authentik Kerberos Source Models"""
import os
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import kadmin
from django.core.cache import cache
from django.db import connection, models
from django.db.models.fields import b64decode
from django.utils.translation import gettext_lazy as _
from redis.lock import Lock
from rest_framework.serializers import Serializer

from authentik.core.models import Source, UserSourceConnection
from authentik.lib.config import CONFIG


class KerberosSource(Source):
    """Federate Kerberos realm with authentik"""

    # python-kadmin leaks file descriptors. As such, this class attribute is used to re-use
    # existing kadmin connections instead of creating new ones, which results in less to no file
    # descriptors leaks
    _kadmin_connections: dict[str, Any] = {}

    realm = models.TextField(help_text=_("Kerberos realm"), unique=True)
    krb5_conf = models.TextField(help_text=_("Kerberos config"), blank=True)

    password_login_enabled = models.BooleanField(default=False)

    sync_users = models.BooleanField(default=True)
    sync_guess_email = models.BooleanField(
        default=False, help_text=_("Try to guess the email from the user principal and realm.")
    )
    sync_users_password = models.BooleanField(
        default=True,
        help_text=_("When a user changes their password, sync it back to Kerberos"),
    )
    sync_principal = models.TextField(
        help_text=_("Principal to authenticate to kadmin for sync"), blank=True
    )
    sync_password = models.TextField(
        help_text=_("Password to authenticate to kadmin for sync"), blank=True
    )
    sync_keytab = models.TextField(
        help_text=_(
            (
                "Keytab to authenticate to kadmin for sync. "
                "Must be base64-encoded or in the form TYPE:residual"
            )
        ),
        blank=True,
    )
    sync_ccache = models.TextField(
        help_text=_(
            (
                "Credentials cache to authenticate to kadmin for sync. "
                "Must be in the form TYPE:residual"
            )
        ),
        blank=True,
    )

    @property
    def component(self) -> str:
        return "ak-source-kerberos-form"

    @property
    def serializer(self) -> type[Serializer]:
        from authentik.sources.kerberos.api.source import KerberosSourceSerializer

        return KerberosSourceSerializer

    def krb5_conf_path(self) -> str | None:
        """Get krb5.conf path"""
        if not self.krb5_conf:
            return None
        # TODO: extract that
        conf_dir = Path(gettempdir()) / "authentik" / "sources" / "kerberos" / str(self.pk)
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_path = conf_dir / "krb5.conf"
        conf_path.write_text(self.krb5_conf)
        return str(conf_path)

    def set_krb5_conf(self) -> str | None:
        """Set the krb5.conf path in the process environment"""
        path = self.krb5_conf_path()
        if not path:
            return None
        previous = os.environ.get("KRB5_CONFIG", None)
        os.environ["KRB5_CONFIG"] = path
        return previous

    def restore_krb5_conf(self, previous: str | None):
        """Restore the krb5.conf path in the process environment"""
        if previous:
            os.environ["KRB5_CONFIG"] = previous
        else:
            del os.environ["KRB5_CONFIG"]

    def _kadmin_init(self) -> kadmin.KAdmin | None:
        # kadmin doesn't use a ccache for its connection
        # as such, we don't need to create a separate ccache for each source
        if not self.sync_principal:
            return None
        if self.sync_password:
            return kadmin.init_with_password(
                self.sync_principal,
                self.sync_password,
                {},
                self.realm,
            )
        if self.sync_keytab:
            keytab = self.sync_keytab
            if ":" not in keytab:
                # TODO: extract that
                keytab_dir = (
                    Path(gettempdir()) / "authentik" / "sources" / "kerberos" / str(self.pk)
                )
                keytab_dir.mkdir(parents=True, exist_ok=True)
                keytab_path = keytab_dir / "keytab"
                # TODO: proper permissions
                keytab_path.write_bytes(b64decode(self.keytab))
                keytab = f"FILE:{keytab_path}"
            return kadmin.init_with_keytab(
                self.sync_principal,
                keytab,
                {},
                self.realm,
            )
        if self.sync_ccache:
            return kadmin.init_with_ccache(
                self.sync_principal,
                self.sync_ccache,
                {},
                self.realm,
            )
        return None

    def connection(self) -> kadmin.KAdmin | None:
        """Get kadmin connection"""
        if str(self.pk) not in self._kadmin_connections:
            kadm = self._kadmin_init()
            if kadm is not None:
                self._kadmin_connections[str(self.pk)] = self._kadmin_init()
        return self._kadmin_connections.get(str(self.pk), None)

    @property
    def sync_lock(self) -> Lock:
        """Redis lock for syncing Kerberos to prevent multiple parallel syncs happening"""
        return Lock(
            cache.client.get_client(),
            name=f"goauthentik.io/sources/kerberos/sync/{connection.schema_name}-{self.slug}",
            # Convert task timeout hours to seconds, and multiply times 3
            # (see authentik/sources/kerberos/tasks.py:54)
            # multiply by 3 to add even more leeway
            timeout=(60 * 60 * CONFIG.get_int("kerberos.task_timeout_hours")) * 3,
        )

    def check_connection(self) -> dict[str, str]:
        """Check Kerberos Connection"""
        status = {"status": "ok"}
        if not self.sync_users:
            return status
        with Krb5ConfContext(self):
            try:
                kadm = self.connection()
                if kadm is None:
                    status["status"] = "no connection"
                    return status
                status["principal_exists"] = kadm.principal_exists(self.sync_principal)
            except kadmin.Error as exc:
                status["status"] = str(exc)
        return status

    class Meta:
        verbose_name = _("Kerberos Source")
        verbose_name_plural = _("Kerberos Sources")


class Krb5ConfContext:
    """
    Context manager to set the path to the krb5.conf config file.
    """
    def __init__(self, source: KerberosSource):
        self._source = source
        self._previous = None

    def __enter__(self):
        self._previous = self._source.set_krb5_conf()

    def __exit__(self, *args, **kwargs):
        self._source.restore_krb5_conf(self._previous)


class UserKerberosSourceConnection(UserSourceConnection):
    """Connection to configured Kerberos Sources."""

    identifier = models.TextField()

    @property
    def serializer(self) -> Serializer:
        from authentik.sources.kerberos.api.source_connection import (
            UserKerberosSourceConnectionSerializer,
        )

        return UserKerberosSourceConnectionSerializer

    class Meta:
        verbose_name = _("User Kerberos Source Connection")
        verbose_name_plural = _("User Kerberos Source Connections")
