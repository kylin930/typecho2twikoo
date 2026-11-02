"""Read-only DNS → TCP → verified TLS → authenticated Mongo ping diagnostics."""
from __future__ import annotations

import socket
import ssl
from typing import Any

import dns.exception
import dns.resolver

from .mongodb import Settings, classify, connect


def dns_query(name: str, kind: str, timeout: float) -> tuple[list[Any], str]:
    try:
        return list(dns.resolver.resolve(name, kind, lifetime=timeout)), 'ok'
    except dns.resolver.NXDOMAIN:
        return [], 'nxdomain'
    except dns.resolver.NoAnswer:
        return [], 'no_answer'
    except dns.exception.Timeout:
        return [], 'timeout'
    except (dns.exception.DNSException, OSError):
        return [], 'resolver_error'


def diagnose(settings: Settings) -> dict[str, Any]:
    timeout = settings.connect_ms / 1000
    result: dict[str, Any] = {'target': settings.public(), 'nodes': [], 'ping': 'unrun',
        'authenticated': False, 'writes_performed': False,
        'ip_access_list': 'not_observable_from_client; verify actual egress IP in Atlas, do not broaden automatically'}
    nodes = settings.seeds
    if settings.srv:
        seed = nodes[0][0]
        srv, srv_status = dns_query('_mongodb._tcp.' + seed, 'SRV', timeout)
        txt, txt_status = dns_query(seed, 'TXT', timeout)
        result['srv'] = {'status': srv_status, 'records': len(srv)}
        result['txt'] = {'status': txt_status, 'records': len(txt)}
        if not srv or txt_status not in {'ok', 'no_answer'}:
            result['category'] = 'dns'
            return result
        parent = seed.split('.', 1)[-1].lower()
        nodes = [(str(r.target).rstrip('.'), int(r.port)) for r in srv]
        if any(not host.lower().endswith('.' + parent) for host, _ in nodes):
            result['category'] = 'dns_untrusted_srv_target'
            return result
    result['nodes_truncated'] = len(nodes) > 12
    ready = False
    for index, (host, port) in enumerate(nodes[:12], 1):
        # Report only aliases/counts, never URI/credentials or TXT content.
        node: dict[str, Any] = {'node': index, 'port': port}
        for kind in ['A', 'AAAA']:
            answers, status = dns_query(host, kind, timeout)
            node[kind] = {'status': status, 'records': len(answers)}
        result['nodes'].append(node)
        if not any(node[k]['records'] for k in ['A', 'AAAA']):
            node.update(tcp='unrun', tls='unrun', category='dns')
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                node['tcp'] = 'ok'
                context = ssl.create_default_context(cafile=settings.ca_file)
                with context.wrap_socket(raw, server_hostname=host):
                    node['tls'] = 'ok'
                    ready = True
        except (OSError, ValueError) as exc:
            node.setdefault('tcp', 'failed')
            node['tls'] = 'failed' if node['tcp'] == 'ok' else 'unrun'
            node['category'] = classify(exc).category
    if not ready:
        result['category'] = 'network_or_tls_or_dns'
        return result
    try:
        with connect(settings) as db:
            status = db.command('connectionStatus', showPrivileges=False)
            authenticated = bool(status.get('authInfo', {}).get('authenticatedUsers'))
            result.update(ping='ok', authenticated=authenticated,
                          category='ok' if authenticated else 'authentication_unconfigured')
    except Exception as exc:
        result.update(ping='failed', category=classify(exc).category)
    return result
