"""Network policy for databases the backend reaches directly: public hosts and TLS only."""
import ipaddress
import socket

from database_access.guard import DatabaseAccessError, DatabaseTarget

NETWORK_POLICY_OPTION = "network_policy"
PUBLIC_NETWORK_ONLY = "public"
ALLOWED_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
SSL_MODE_REFUSED = "O modo SSL '{mode}' não é permitido; use require, verify-ca ou verify-full."
HOST_NOT_FOUND_MESSAGE = "Não foi possível conectar ao banco '{alias}': host não encontrado."
PRIVATE_NETWORK_REFUSED = (
    "Não foi possível conectar ao banco '{alias}': o host aponta para um endereço "
    "de rede privada, loopback ou reservado, que não é permitido."
)


def public_network_only(target: DatabaseTarget) -> bool:
    return target.option(NETWORK_POLICY_OPTION) == PUBLIC_NETWORK_ONLY


def check_ssl_mode(target: DatabaseTarget) -> None:
    if target.ssl_mode not in ALLOWED_SSL_MODES:
        raise DatabaseAccessError(SSL_MODE_REFUSED.format(mode=target.ssl_mode))


def resolve_allowed_addresses(target: DatabaseTarget) -> list[str]:
    """Public addresses of the host, refusing any private, loopback or reserved one."""
    try:
        addresses = socket.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DatabaseAccessError(HOST_NOT_FOUND_MESSAGE.format(alias=target.alias)) from exc
    if not addresses:
        raise DatabaseAccessError(HOST_NOT_FOUND_MESSAGE.format(alias=target.alias))
    allowed_addresses: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError as exc:
            raise DatabaseAccessError(HOST_NOT_FOUND_MESSAGE.format(alias=target.alias)) from exc
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise DatabaseAccessError(PRIVATE_NETWORK_REFUSED.format(alias=target.alias))
        if str(ip) not in allowed_addresses:
            allowed_addresses.append(str(ip))
    return allowed_addresses
