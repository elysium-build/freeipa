# Authors: Simo Sorce <ssorce@redhat.com>
#          Alexander Bokovoy <abokovoy@redhat.com>
#          Martin Kosek <mkosek@redhat.com>
#          Tomas Babej <tbabej@redhat.com>
#
# Copyright (C) 2007-2014  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

'''
This module contains default Red Hat OS family-specific implementations of
system tasks.
'''
from __future__ import print_function, absolute_import

import ctypes
import logging
import os
from pathlib import Path
import socket
import traceback
import errno
import urllib
import subprocess
import sys
import textwrap

from ctypes.util import find_library
from functools import total_ordering
from subprocess import CalledProcessError

from pyasn1.error import PyAsn1Error

from ipapython import directivesetter
from ipapython import ipautil
import ipapython.errors

from ipaplatform.constants import constants
from ipaplatform.paths import paths
from ipaplatform.redhat.authconfig import get_auth_tool
from ipaplatform.base.tasks import BaseTaskNamespace

logger = logging.getLogger(__name__)


# /etc/pkcs11/modules override
# base filename, module, list of disabled-in
# 'p11-kit-proxy' disables proxying of module, see man(5) pkcs11.conf
PKCS11_MODULES = [
    ('softhsm2', paths.LIBSOFTHSM2_SO, ['p11-kit-proxy']),
]


NM_IPA_CONF = textwrap.dedent("""
    # auto-generated by IPA installer
    [main]
    dns={dnsprocessing}

    [global-dns]
    searches={searches}

    [global-dns-domain-*]
    servers={servers}
""")


@total_ordering
class IPAVersion:
    _rpmvercmp_func = None

    @classmethod
    def _rpmvercmp(cls, a, b):
        """Lazy load and call librpm's rpmvercmp
        """
        rpmvercmp_func = cls._rpmvercmp_func
        if rpmvercmp_func is None:
            librpm = ctypes.CDLL(find_library('rpm'))
            rpmvercmp_func = librpm.rpmvercmp
            # int rpmvercmp(const char *a, const char *b)
            rpmvercmp_func.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            rpmvercmp_func.restype = ctypes.c_int
            cls._rpmvercmp_func = rpmvercmp_func
        return rpmvercmp_func(a, b)

    def __init__(self, version):
        self._version = version
        self._bytes = version.encode('utf-8')

    @property
    def version(self):
        return self._version

    def __eq__(self, other):
        if not isinstance(other, IPAVersion):
            return NotImplemented
        return self._rpmvercmp(self._bytes, other._bytes) == 0

    def __lt__(self, other):
        if not isinstance(other, IPAVersion):
            return NotImplemented
        return self._rpmvercmp(self._bytes, other._bytes) < 0

    def __hash__(self):
        return hash(self._version)


class RedHatTaskNamespace(BaseTaskNamespace):

    def restore_context(self, filepath, force=False):
        """Restore SELinux security context on the given filepath.

        SELinux equivalent is /path/to/restorecon <filepath>
        restorecon's return values are not reliable so we have to
        ignore them (BZ #739604).

        ipautil.run() will do the logging.
        """
        restorecon = paths.SBIN_RESTORECON
        if not self.is_selinux_enabled() or not os.path.exists(restorecon):
            return

        # Force reset of context to match file_context for customizable
        # files, and the default file context, changing the user, role,
        # range portion as well as the type.
        args = [restorecon]
        if force:
            args.append('-F')
        args.append(filepath)
        ipautil.run(args, raiseonerr=False)

    def is_selinux_enabled(self):
        """Check if SELinux is available and enabled
        """
        try:
            ipautil.run([paths.SELINUXENABLED])
        except ipautil.CalledProcessError:
            # selinuxenabled returns 1 if not enabled
            return False
        except OSError:
            # selinuxenabled binary not available
            return False
        else:
            return True

    def check_selinux_status(self, restorecon=paths.RESTORECON):
        """
        We don't have a specific package requirement for policycoreutils
        which provides restorecon. This is because we don't require
        SELinux on client installs. However if SELinux is enabled then
        this package is required.

        This function returns nothing but may raise a Runtime exception
        if SELinux is enabled but restorecon is not available.
        """
        if not self.is_selinux_enabled():
            return False

        if not os.path.exists(restorecon):
            raise RuntimeError('SELinux is enabled but %s does not exist.\n'
                               'Install the policycoreutils package and start '
                               'the installation again.' % restorecon)
        return True

    def check_ipv6_stack_enabled(self):
        """Checks whether IPv6 kernel module is loaded.

        Function checks if /proc/net/if_inet6 is present. If IPv6 stack is
        enabled, it exists and contains the interfaces configuration.

        :raises: RuntimeError when IPv6 stack is disabled
        """
        if not os.path.exists(paths.IF_INET6):
            raise RuntimeError(
                "IPv6 stack has to be enabled in the kernel and some "
                "interface has to have ::1 address assigned. Typically "
                "this is 'lo' interface. If you do not wish to use IPv6 "
                "globally, disable it on the specific interfaces in "
                "sysctl.conf except 'lo' interface.")

        try:
            localhost6 = ipautil.CheckedIPAddress('::1', allow_loopback=True)
            if localhost6.get_matching_interface() is None:
                raise ValueError("no interface for ::1 address found")
        except ValueError:
            raise RuntimeError(
                 "IPv6 stack is enabled in the kernel but there is no "
                 "interface that has ::1 address assigned. Add ::1 address "
                 "resolution to 'lo' interface. You might need to enable IPv6 "
                 "on the interface 'lo' in sysctl.conf.")

    def detect_container(self):
        """Check if running inside a container

        :returns: container runtime or None
        :rtype: str, None
        """
        try:
            output = subprocess.check_output(
                [paths.SYSTEMD_DETECT_VIRT, '--container'],
                stderr=subprocess.STDOUT
            )
        except subprocess.CalledProcessError as e:
            if e.returncode == 1:
                # No container runtime detected
                return None
            else:
                raise
        else:
            return output.decode('utf-8').strip()

    def restore_pre_ipa_client_configuration(self, fstore, statestore,
                                             was_sssd_installed,
                                             was_sssd_configured):

        auth_config = get_auth_tool()
        auth_config.unconfigure(
            fstore, statestore, was_sssd_installed, was_sssd_configured
        )

    def set_nisdomain(self, nisdomain):
        try:
            with open(paths.SYSCONF_NETWORK, 'r') as f:
                content = [
                    line for line in f
                    if not line.strip().upper().startswith('NISDOMAIN')
                ]
        except IOError:
            content = []

        content.append("NISDOMAIN={}\n".format(nisdomain))

        with open(paths.SYSCONF_NETWORK, 'w') as f:
            f.writelines(content)

    def modify_nsswitch_pam_stack(self, sssd, mkhomedir, statestore,
                                  sudo=True):
        auth_config = get_auth_tool()
        auth_config.configure(sssd, mkhomedir, statestore, sudo)

    def is_nosssd_supported(self):
        # The flag --no-sssd is not supported any more for rhel-based distros
        return False

    def backup_auth_configuration(self, path):
        auth_config = get_auth_tool()
        auth_config.backup(path)

    def restore_auth_configuration(self, path):
        auth_config = get_auth_tool()
        auth_config.restore(path)

    def migrate_auth_configuration(self, statestore):
        """
        Migrate the pam stack configuration from authconfig to an authselect
        profile.
        """
        # Check if mkhomedir was enabled during installation
        mkhomedir = statestore.get_state('authconfig', 'mkhomedir')

        # Force authselect 'sssd' profile
        authselect_cmd = [paths.AUTHSELECT, "select", "sssd", "with-sudo"]
        if mkhomedir:
            authselect_cmd.append("with-mkhomedir")
        authselect_cmd.append("--force")
        ipautil.run(authselect_cmd)

        # Remove all remaining keys from the authconfig module
        for conf in ('ldap', 'krb5', 'sssd', 'sssdauth', 'mkhomedir'):
            statestore.restore_state('authconfig', conf)

        # Create new authselect module in the statestore
        statestore.backup_state('authselect', 'profile', 'sssd')
        statestore.backup_state(
            'authselect', 'features_list', '')
        statestore.backup_state('authselect', 'mkhomedir', bool(mkhomedir))

    def reload_systemwide_ca_store(self):
        try:
            ipautil.run([paths.UPDATE_CA_TRUST])
        except CalledProcessError as e:
            logger.error(
                "Could not update systemwide CA trust database: %s", e)
            return False
        else:
            logger.info("Systemwide CA database updated.")
            return True

    def platform_insert_ca_certs(self, ca_certs):
        return any([
            self.write_p11kit_certs(paths.IPA_P11_KIT, ca_certs),
            self.remove_ca_certificates_bundle(
                paths.SYSTEMWIDE_IPA_CA_CRT
            ),
        ])

    def write_p11kit_certs(self, filename, ca_certs):
        # pylint: disable=ipa-forbidden-import
        from ipalib import x509  # FixMe: break import cycle
        from ipalib.errors import CertificateError
        # pylint: enable=ipa-forbidden-import

        path = Path(filename)
        try:
            f = open(path, 'w')
        except IOError:
            logger.error("Failed to open %s", path)
            raise

        with f:
            f.write("# This file was created by IPA. Do not edit.\n"
                    "\n")

            try:
                os.fchmod(f.fileno(), 0o644)
            except IOError:
                logger.error("Failed to set mode of %s", path)
                raise

            has_eku = set()
            for cert, nickname, trusted, _ext_key_usage in ca_certs:
                try:
                    subject = cert.subject_bytes
                    issuer = cert.issuer_bytes
                    serial_number = cert.serial_number_bytes
                    public_key_info = cert.public_key_info_bytes
                except (PyAsn1Error, ValueError, CertificateError):
                    logger.error(
                        "Failed to decode certificate \"%s\"", nickname)
                    raise

                label = urllib.parse.quote(nickname)
                subject = urllib.parse.quote(subject)
                issuer = urllib.parse.quote(issuer)
                serial_number = urllib.parse.quote(serial_number)
                public_key_info = urllib.parse.quote(public_key_info)

                obj = ("[p11-kit-object-v1]\n"
                       "class: certificate\n"
                       "certificate-type: x-509\n"
                       "certificate-category: authority\n"
                       "label: \"%(label)s\"\n"
                       "subject: \"%(subject)s\"\n"
                       "issuer: \"%(issuer)s\"\n"
                       "serial-number: \"%(serial_number)s\"\n"
                       "x-public-key-info: \"%(public_key_info)s\"\n" %
                       dict(label=label,
                            subject=subject,
                            issuer=issuer,
                            serial_number=serial_number,
                            public_key_info=public_key_info))
                if trusted is True:
                    obj += "trusted: true\n"
                elif trusted is False:
                    obj += "x-distrusted: true\n"
                obj += "{pem}\n\n".format(
                    pem=cert.public_bytes(x509.Encoding.PEM).decode('ascii'))

                f.write(obj)

                if (cert.extended_key_usage is not None and
                        public_key_info not in has_eku):
                    try:
                        ext_key_usage = cert.extended_key_usage_bytes
                    except PyAsn1Error:
                        logger.error(
                            "Failed to encode extended key usage for \"%s\"",
                            nickname)
                        raise
                    value = urllib.parse.quote(ext_key_usage)
                    obj = ("[p11-kit-object-v1]\n"
                           "class: x-certificate-extension\n"
                           "label: \"ExtendedKeyUsage for %(label)s\"\n"
                           "x-public-key-info: \"%(public_key_info)s\"\n"
                           "object-id: 2.5.29.37\n"
                           "value: \"%(value)s\"\n\n" %
                           dict(label=label,
                                public_key_info=public_key_info,
                                value=value))
                    f.write(obj)
                    has_eku.add(public_key_info)

        return True

    def platform_remove_ca_certs(self):
        return any([
            self.remove_ca_certificates_bundle(paths.IPA_P11_KIT),
            self.remove_ca_certificates_bundle(paths.SYSTEMWIDE_IPA_CA_CRT),
        ])

    def remove_ca_certificates_bundle(self, filename):
        path = Path(filename)
        if not path.is_file():
            return False

        try:
            path.unlink()
        except Exception:
            logger.error("Could not remove %s", path)
            raise

        return True

    def backup_hostname(self, fstore, statestore):
        filepath = paths.ETC_HOSTNAME
        if os.path.exists(filepath):
            fstore.backup_file(filepath)

        # store old hostname
        old_hostname = socket.gethostname()
        statestore.backup_state('network', 'hostname', old_hostname)

    def restore_hostname(self, fstore, statestore):
        old_hostname = statestore.restore_state('network', 'hostname')

        if old_hostname is not None:
            try:
                self.set_hostname(old_hostname)
            except ipautil.CalledProcessError as e:
                logger.debug("%s", traceback.format_exc())
                logger.error(
                    "Failed to restore this machine hostname to %s (%s).",
                    old_hostname, e
                )

        filepath = paths.ETC_HOSTNAME
        if fstore.has_file(filepath):
            fstore.restore_file(filepath)

    def set_selinux_booleans(self, required_settings, backup_func=None):
        def get_setsebool_args(changes):
            args = [paths.SETSEBOOL, "-P"]
            args.extend(["%s=%s" % update for update in changes.items()])

            return args

        if not self.is_selinux_enabled():
            return False

        updated_vars = {}
        failed_vars = {}
        for setting, state in required_settings.items():
            if state is None:
                continue
            try:
                result = ipautil.run(
                    [paths.GETSEBOOL, setting],
                    capture_output=True
                )
                original_state = result.output.split()[2]
                if backup_func is not None:
                    backup_func(setting, original_state)

                if original_state != state:
                    updated_vars[setting] = state
            except ipautil.CalledProcessError as e:
                logger.error("Cannot get SELinux boolean '%s': %s", setting, e)
                failed_vars[setting] = state

        if updated_vars:
            args = get_setsebool_args(updated_vars)
            try:
                ipautil.run(args)
            except ipautil.CalledProcessError:
                failed_vars.update(updated_vars)

        if failed_vars:
            raise ipapython.errors.SetseboolError(
                failed=failed_vars,
                command=' '.join(get_setsebool_args(failed_vars)))

        return True

    def parse_ipa_version(self, version):
        """
        :param version: textual version
        :return: object implementing proper __cmp__ method for version compare
        """
        return IPAVersion(version)

    def configure_httpd_service_ipa_conf(self):
        """Create systemd config for httpd service to work with IPA
        """
        if not os.path.exists(paths.SYSTEMD_SYSTEM_HTTPD_D_DIR):
            os.mkdir(paths.SYSTEMD_SYSTEM_HTTPD_D_DIR, 0o755)

        ipautil.copy_template_file(
            os.path.join(paths.USR_SHARE_IPA_DIR, 'ipa-httpd.conf.template'),
            paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF,
            dict(
                KDCPROXY_CONFIG=paths.KDCPROXY_CONFIG,
                IPA_HTTPD_KDCPROXY=paths.IPA_HTTPD_KDCPROXY,
                KRB5CC_HTTPD=paths.KRB5CC_HTTPD,
            )
        )

        os.chmod(paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF, 0o644)
        self.restore_context(paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF)
        self.systemd_daemon_reload()

    def systemd_daemon_reload(self):
        """Tell systemd to reload config files"""
        ipautil.run([paths.SYSTEMCTL, "--system", "daemon-reload"])

    def configure_http_gssproxy_conf(self, ipaapi_user):
        ipautil.copy_template_file(
            os.path.join(paths.USR_SHARE_IPA_DIR, 'gssproxy.conf.template'),
            paths.GSSPROXY_CONF,
            dict(
                HTTP_KEYTAB=paths.HTTP_KEYTAB,
                HTTPD_USER=constants.HTTPD_USER,
                IPAAPI_USER=ipaapi_user,
                SWEEPER_SOCKET=paths.IPA_CCACHE_SWEEPER_GSSPROXY_SOCK,
            )
        )

        os.chmod(paths.GSSPROXY_CONF, 0o600)
        self.restore_context(paths.GSSPROXY_CONF)

    def configure_httpd_wsgi_conf(self):
        """Configure WSGI for correct Python version (Fedora)

        See https://pagure.io/freeipa/issue/7394
        """
        conf = paths.HTTPD_IPA_WSGI_MODULES_CONF
        if sys.version_info.major == 2:
            wsgi_module = constants.MOD_WSGI_PYTHON2
        else:
            wsgi_module = constants.MOD_WSGI_PYTHON3

        if conf is None or wsgi_module is None:
            logger.info("Nothing to do for configure_httpd_wsgi_conf")
            return

        confdir = os.path.dirname(conf)
        if not os.path.isdir(confdir):
            os.makedirs(confdir)

        ipautil.copy_template_file(
            os.path.join(
                paths.USR_SHARE_IPA_DIR, 'ipa-httpd-wsgi.conf.template'
            ),
            conf,
            dict(WSGI_MODULE=wsgi_module)
        )

        os.chmod(conf, 0o644)
        self.restore_context(conf)

    def remove_httpd_service_ipa_conf(self):
        """Remove systemd config for httpd service of IPA"""
        try:
            os.unlink(paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF)
        except OSError as e:
            if e.errno == errno.ENOENT:
                logger.debug(
                    'Trying to remove %s but file does not exist',
                    paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF
                )
            else:
                logger.error(
                    'Error removing %s: %s',
                    paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF, e
                )
            return

        self.systemd_daemon_reload()

    def configure_httpd_protocol(self):
        # use default crypto policy for SSLProtocol
        directivesetter.set_directive(
            paths.HTTPD_SSL_CONF, 'SSLProtocol', None, False
        )

    def set_hostname(self, hostname):
        ipautil.run([paths.BIN_HOSTNAMECTL, 'set-hostname', hostname])

    def is_fips_enabled(self):
        """
        Checks whether this host is FIPS-enabled.

        Returns a boolean indicating if the host is FIPS-enabled, i.e. if the
        file /proc/sys/crypto/fips_enabled contains a non-0 value. Otherwise,
        or if the file /proc/sys/crypto/fips_enabled does not exist,
        the function returns False.
        """
        try:
            with open(paths.PROC_FIPS_ENABLED, 'r') as f:
                if f.read().strip() != '0':
                    return True
        except IOError:
            # Consider that the host is not fips-enabled if the file does not
            # exist
            pass
        return False

    def setup_httpd_logging(self):
        directivesetter.set_directive(paths.HTTPD_SSL_CONF,
                                      'ErrorLog',
                                      'logs/error_log', False)
        directivesetter.set_directive(paths.HTTPD_SSL_CONF,
                                      'TransferLog',
                                      'logs/access_log', False)

    def configure_dns_resolver(self, nameservers, searchdomains, *,
                               resolve1_enabled=False, fstore=None):
        """Configure global DNS resolver (e.g. /etc/resolv.conf)

        :param nameservers: list of IP addresses
        :param searchdomains: list of search domaons
        :param fstore: optional file store for backup
        """
        assert nameservers and isinstance(nameservers, list)
        assert searchdomains and isinstance(searchdomains, list)

        super().configure_dns_resolver(
            nameservers=nameservers,
            searchdomains=searchdomains,
            resolve1_enabled=resolve1_enabled,
            fstore=fstore
        )

        # break circular import
        from ipaplatform.services import knownservices

        if fstore is not None and not fstore.has_file(paths.RESOLV_CONF):
            fstore.backup_file(paths.RESOLV_CONF)

        nm = knownservices['NetworkManager']
        nm_enabled = nm.is_enabled()
        if nm_enabled:
            logger.debug(
                "Network Manager is enabled, write %s",
                paths.NETWORK_MANAGER_IPA_CONF
            )
            # write DNS override and reload network manager to have it create
            # a new resolv.conf. The file is prefixed with ``zzz`` to
            # make it the last file. Global dns options do not stack and last
            # man standing wins.
            if resolve1_enabled:
                # push DNS configuration to systemd-resolved
                dnsprocessing = "systemd-resolved"
            else:
                # update /etc/resolv.conf
                dnsprocessing = "default"

            cfg = NM_IPA_CONF.format(
                dnsprocessing=dnsprocessing,
                servers=','.join(nameservers),
                searches=','.join(searchdomains)
            )
            with open(paths.NETWORK_MANAGER_IPA_CONF, 'w') as f:
                os.fchmod(f.fileno(), 0o644)
                f.write(cfg)
            # reload NetworkManager
            nm.reload_or_restart()

        if not resolve1_enabled and not nm_enabled:
            # no NM running, no systemd-resolved detected
            # fall back to /etc/resolv.conf
            logger.debug(
                "Neither Network Manager nor systemd-resolved are enabled, "
                "write %s directly.", paths.RESOLV_CONF
            )
            cfg = [
                "# auto-generated by IPA installer",
                "search {}".format(' '.join(searchdomains)),
            ]
            for nameserver in nameservers:
                cfg.append("nameserver {}".format(nameserver))
            with open(paths.RESOLV_CONF, 'w') as f:
                f.write('\n'.join(cfg))

    def unconfigure_dns_resolver(self, fstore=None):
        """Unconfigure global DNS resolver (e.g. /etc/resolv.conf)

        :param fstore: optional file store for restore
        """
        super().unconfigure_dns_resolver(fstore=fstore)
        # break circular import
        from ipaplatform.services import knownservices

        nm = knownservices['NetworkManager']
        if os.path.isfile(paths.NETWORK_MANAGER_IPA_CONF):
            os.unlink(paths.NETWORK_MANAGER_IPA_CONF)
            if nm.is_enabled():
                nm.reload_or_restart()

    def configure_pkcs11_modules(self, fstore):
        """Disable global p11-kit configuration for NSS
        """
        filenames = []
        for name, module, disabled_in in PKCS11_MODULES:
            filename = os.path.join(
                paths.ETC_PKCS11_MODULES_DIR,
                "{}.module".format(name)
            )
            if os.path.isfile(filename):
                # Only back up if file is not yet backed up and it does not
                # look like a file that is generated by IPA.
                with open(filename) as f:
                    content = f.read()
                is_ipa_file = "IPA" in content
                if not is_ipa_file and not fstore.has_file(filename):
                    logger.debug("Backing up existing '%s'.", filename)
                    fstore.backup_file(filename)

            with open(filename, "w") as f:
                f.write("# created by IPA installer\n")
                f.write("module: {}\n".format(module))
                # see man(5) pkcs11.conf
                f.write("disable-in: {}\n".format(", ".join(disabled_in)))
                os.fchmod(f.fileno(), 0o644)
            self.restore_context(filename)
            logger.debug("Created PKCS#11 module config '%s'.", filename)
            filenames.append(filename)

        return filenames

    def restore_pkcs11_modules(self, fstore):
        """Restore global p11-kit configuration for NSS
        """
        filenames = []
        for name, _module, _disabled_in in PKCS11_MODULES:
            filename = os.path.join(
                paths.ETC_PKCS11_MODULES_DIR,
                "{}.module".format(name)
            )
            try:
                os.unlink(filename)
            except OSError:
                pass
            else:
                filenames.append(filename)

            if fstore.has_file(filename):
                fstore.restore_file(filename)

        return filenames

    def get_pkcs11_modules(self):
        """Return the list of module config files setup by IPA
        """
        return tuple(os.path.join(paths.ETC_PKCS11_MODULES_DIR,
                                  "{}.module".format(name))
                     for name, _module, _disabled in PKCS11_MODULES)

    def enable_ldap_automount(self, statestore):
        """
        Point automount to ldap in nsswitch.conf.
        This function is for non-SSSD setups only.
        """
        super(RedHatTaskNamespace, self).enable_ldap_automount(statestore)

        authselect_cmd = [paths.AUTHSELECT, "enable-feature",
                          "with-custom-automount"]
        ipautil.run(authselect_cmd)

    def disable_ldap_automount(self, statestore):
        """Disable ldap-based automount"""
        super(RedHatTaskNamespace, self).disable_ldap_automount(statestore)

        authselect_cmd = [paths.AUTHSELECT, "disable-feature",
                          "with-custom-automount"]
        ipautil.run(authselect_cmd)

tasks = RedHatTaskNamespace()
