from datetime import timedelta
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db.models import Q
from django.utils import timezone


_RASTREIO_ML = ("matt_word", "matt_tool", "tracking_id")
_RASTREIO_POR_USUARIO = {}
_TTL_HIT_S = 3600.0
_TTL_MISS_S = 120.0


def _chave_usuario(usuario):
    """Identidade de cache que não sobrevive à recriação de uma conta.

    Usar somente ``pk`` deixa um valor antigo ser entregue a outra conta quando
    uma base é restaurada, testes revertem transações ou um usuário é apagado e
    o identificador volta a ser usado. ``date_joined`` distingue essas vidas sem
    transformar cada instância carregada pelo ORM numa chave diferente.
    """
    if usuario is None:
        return None
    joined = getattr(usuario, "date_joined", None)
    joined_key = joined.isoformat() if joined is not None else ""
    return (getattr(getattr(usuario, "_state", None), "db", None),
            getattr(usuario, "pk", None), joined_key)


def coupon_link_verified_and_fresh(link, *, now=None) -> bool:
    """True somente para cache de cupom aprovado e dentro do TTL operacional."""
    if link is None or link.verificado_ok is not True:
        return False
    if not (link.url_canonica or link.link_afiliado):
        return False
    # Caches históricos de produto já tinham veredito, mas a coluna
    # ``verificado_em`` só passou a ser obrigatória depois. ``criado_em`` (ou o
    # timestamp de atualização do cache de cupom) é o limite conservador durante
    # a janela de migração; links novos sempre gravam ``verificado_em``.
    verified_at = (
        link.verificado_em
        or getattr(link, "atualizado_em", None)
        or getattr(link, "criado_em", None)
    )
    if verified_at is None:
        return False
    ttl_hours = int(getattr(settings, "COUPON_AFFILIATE_LINK_TTL_HOURS", 168) or 168)
    return verified_at >= (now or timezone.now()) - timedelta(hours=max(1, ttl_hours))


def canonical_coupon_link(link) -> str:
    return str(getattr(link, "url_canonica", "") or getattr(link, "link_afiliado", "") or "")


def _params_rastreio_ml(url: str) -> dict:
    try:
        query = dict(parse_qsl(urlsplit(str(url or "")).query, keep_blank_values=True))
    except ValueError:
        return {}
    achados = {}
    for chave in _RASTREIO_ML:
        valor = str(query.get(chave) or "").strip()
        if valor:
            achados[chave] = valor
    return achados


def _q_tem_rastreio():
    q = Q()
    for chave in _RASTREIO_ML:
        token = f"{chave}="
        q |= Q(link_afiliado__icontains=token)
        q |= Q(url_canonica__icontains=token)
    return q


def _params_de_linha(linha) -> dict:
    for bruto in (getattr(linha, "url_canonica", ""), getattr(linha, "link_afiliado", "")):
        achados = _params_rastreio_ml(bruto)
        if achados:
            return achados
    return {}


def _encolher_canonica(url: str, limite: int) -> str:
    """Guarda host+path+rastreio. O `ref` cifrado do ML estoura URLField."""
    params = _params_rastreio_ml(url)
    if not params:
        return str(url or "")[:limite]
    try:
        partes = urlsplit(url)
    except ValueError:
        return str(url or "")[:limite]
    return urlunsplit((
        partes.scheme, partes.netloc, partes.path, urlencode(params), "",
    ))[:limite]


def url_canonica_com_rastreio(relatorio, fallback="") -> str:
    """Destino do relatório só entra no cache se trouxer matt_word/matt_tool."""
    dest = str((relatorio or {}).get("url_final") or "")
    if not _params_rastreio_ml(dest):
        return fallback
    return _encolher_canonica(dest, 1000)


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _eh_encurtador_ml(url: str) -> bool:
    host = _host(url)
    return host == "meli.la" or host == "mercadolivre.com" or host.endswith(
        ".mercadolivre.com",
    )


def _expandir_encurtador_ml(url: str):
    """Um hop HTTP. Fly costuma cair em interstitial; Chromium é o plano B."""
    if not _eh_encurtador_ml(url):
        return {}, ""
    import requests
    from apps.scrapers.scraper_mercadolivre.link_http import _get_ml

    try:
        sessao = requests.Session()
        sessao.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
            ),
        })
        resposta = _get_ml(sessao, url, timeout=8)
    except Exception:
        return {}, ""
    cadeia = [
        str((getattr(hop, "headers", {}) or {}).get("location") or "")
        for hop in (getattr(resposta, "history", None) or [])
    ]
    cadeia.append(str(getattr(resposta, "url", "") or ""))
    for pedaco in cadeia:
        params = _params_rastreio_ml(pedaco)
        if params:
            return params, pedaco
    return {}, ""


def _persistir_rastreio(linha, url: str) -> None:
    params = _params_rastreio_ml(url)
    if linha is None or not params:
        return
    lim = getattr(linha._meta.get_field("url_canonica"), "max_length", None) or 1000
    canonica = _encolher_canonica(url, lim)
    if (linha.url_canonica or "") == canonica:
        return
    type(linha).objects.filter(pk=linha.pk).update(url_canonica=canonica)
    linha.url_canonica = canonica


def _cache_ler(chave):
    item = _RASTREIO_POR_USUARIO.get(chave)
    if item is None:
        return None, False
    ts, valor = item
    ttl = _TTL_HIT_S if valor else _TTL_MISS_S
    if time.monotonic() - ts < ttl:
        return valor, True
    del _RASTREIO_POR_USUARIO[chave]
    return None, False


def _cache_gravar(chave, valor) -> None:
    if chave is None:
        return
    if len(_RASTREIO_POR_USUARIO) > 64:
        _RASTREIO_POR_USUARIO.clear()
    _RASTREIO_POR_USUARIO[chave] = (time.monotonic(), valor)


def _cache_apagar(chave) -> None:
    if chave is not None:
        _RASTREIO_POR_USUARIO.pop(chave, None)


def _linha_com_rastreio(usuario):
    from apps.scrapers.models import LinkAfiliadoCupomUsuario, LinkAfiliadoUsuario

    filtro = _q_tem_rastreio()
    return (
        LinkAfiliadoCupomUsuario.objects.filter(
            usuario=usuario, verificado_ok=True,
        ).filter(filtro).order_by("-verificado_em").first()
        or LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, verificado_ok=True,
            produto__marketplace="mercadolivre",
        ).filter(filtro).order_by("-verificado_em").first()
    )


def _linha_encurtador(usuario):
    from apps.scrapers.models import LinkAfiliadoCupomUsuario, LinkAfiliadoUsuario

    for linha in LinkAfiliadoCupomUsuario.objects.filter(
            usuario=usuario, verificado_ok=True,
    ).exclude(link_afiliado="").order_by("-verificado_em")[:8]:
        if _eh_encurtador_ml(linha.url_canonica or linha.link_afiliado):
            return linha
    for linha in LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, verificado_ok=True,
            produto__marketplace="mercadolivre",
    ).exclude(link_afiliado="").order_by("-verificado_em")[:8]:
        if _eh_encurtador_ml(getattr(linha, "url_canonica", "") or linha.link_afiliado):
            return linha
    return None


def rastreio_afiliado_ml(usuario, *, forcar=False) -> dict:
    """Params de tracking já provados num link ML deste usuário.

    O Link Builder (Chromium) é a única fábrica. Daí em diante copiar
    `matt_word`/`matt_tool`/`tracking_id` para a listagem pública da campanha é
    o mesmo que `?tag=` na Amazon: atribuição sem abrir navegador. Sem um link
    verificado na conta, não inventa parâmetro.

    Cache por usuário: `_ativacao` chama isto em cada campanha; sem cache a
    projeção virava milhares de SELECTs iguais. Miss dura 2 min (próximo ciclo
    do worker pode ter ganhado `url_canonica` com rastreio).
    """
    if usuario is None:
        return {}
    chave = _chave_usuario(usuario)
    if forcar:
        _cache_apagar(chave)
    elif chave is not None:
        valor, hit = _cache_ler(chave)
        if hit:
            return valor

    linha = _linha_com_rastreio(usuario)
    if linha:
        achados = _params_de_linha(linha)
        _cache_gravar(chave, achados)
        return achados

    linha = _linha_encurtador(usuario)
    if linha:
        url = linha.url_canonica or linha.link_afiliado
        achados, destino = _expandir_encurtador_ml(url)
        if achados:
            _persistir_rastreio(linha, destino)
            _cache_gravar(chave, achados)
            return achados

    _cache_gravar(chave, {})
    return {}


def colher_rastreio_ml_browser(usuario) -> dict:
    """Um Chromium, um meli.la. HTTP da Fly cai em interstitial; browser não.

    Só corre se a conta já tem link verificado e ainda não tem matt_word no
    banco. Não inventa parâmetro.
    """
    achados = rastreio_afiliado_ml(usuario, forcar=True)
    if achados or usuario is None:
        return achados
    linha = _linha_encurtador(usuario)
    if linha is None:
        return {}
    url = linha.url_canonica or linha.link_afiliado
    try:
        from apps.scrapers.scraper_mercadolivre.link import _verificar_com_browser
        relatorio = _verificar_com_browser(url, None, None, True)
    except Exception:
        return {}
    destino = str((relatorio or {}).get("url_final") or "")
    achados = _params_rastreio_ml(destino)
    if not achados:
        return {}
    _persistir_rastreio(linha, destino)
    chave = _chave_usuario(usuario)
    _cache_apagar(chave)
    _cache_gravar(chave, achados)
    return achados


def gerar_link_afiliado_listagem_ml(cupom, usuario=None) -> str:
    """Listagem pública da campanha + tracking do usuário. Sem Chromium."""
    from apps.scrapers.coupon_rules import listagem_publica_ml

    base = listagem_publica_ml(cupom)
    if not base:
        return ""
    rastreio = rastreio_afiliado_ml(usuario)
    if not rastreio:
        return ""
    try:
        partes = urlsplit(base)
    except ValueError:
        return ""
    query = dict(parse_qsl(partes.query, keep_blank_values=True))
    query.update(rastreio)
    return urlunsplit((
        partes.scheme, partes.netloc, partes.path, urlencode(query), partes.fragment,
    ))
