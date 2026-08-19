"""Prova de que a oferta de um canal de terceiro é real, antes de ela sair.

O worker de canais existia como espelho puro: pegava a mensagem do canal-fonte,
trocava os links pela tag do dono e reenviava o texto **do jeito que estava**. Isso
transfere para o grupo do usuário toda alegação de preço, estoque e desconto escrita
por um estranho — e quem opera o Spreading são influenciadores cuja reputação está
em cada mensagem. Uma oferta esgotada, um "de R$ 500 por R$ 99" inventado ou um
anúncio pausado chegam com a cara do usuário, não do canal-fonte.

Aqui a mensagem passa a ser tratada como **pista**, não como conteúdo pronto. A
regra é uma só:

    nenhum link sai sem que o destino tenha sido aberto e aprovado agora.

Quem dá o veredito é o MESMO verificador do fluxo normal — ``Marketplace.verify_link``
via ``link_validacao.aprovado_por_relatorio``. Não existe um segundo critério de
aprovação para o caminho dos canais: dois critérios divergentes seriam duas
definições de "oferta boa", e a mais frouxa acabaria virando a real.

Três decisões que valem explicação:

* **Falha de verificação não vira reprovação.** Bloqueio de anti-bot, timeout e
  interstitial devolvem "não sei", e "não sei" segura a mensagem para o próximo
  ciclo em vez de queimá-la. O contrário derrubaria mensagens boas em lote sempre
  que o marketplace apertasse a mão.
* **Mensagem parcial não é enviada.** Se um dos links não passa, a mensagem inteira
  fica de fora. Reescrever o texto de terceiro para remover só o item reprovado
  produziria uma mensagem que ninguém escreveu, com preço solto sem produto.
* **O texto continua sendo o do canal.** Este módulo não melhora a mensagem, só
  decide se ela pode sair. Reconstruir a mensagem a partir do nosso próprio catálogo
  é o passo seguinte e maior — e é o que finalmente tira a dependência do texto
  alheio.
"""
import logging

logger = logging.getLogger(__name__)

# Vereditos possíveis de um link.
APROVADO = "aprovado"
REPROVADO = "reprovado"
INCERTO = "incerto"


def _marketplace_da_url(url):
    texto = str(url or "").lower()
    if "mercadolivre.com" in texto or "mercadolibre.com" in texto or "meli.la" in texto:
        return "mercadolivre"
    if "amazon.com" in texto or "amzn.to" in texto or "amzn.eu" in texto:
        return "amazon"
    if "shopee.com.br" in texto or "s.shopee.com.br" in texto or "shope.ee" in texto:
        return "shopee"
    return ""


def verificar_link(url_afiliado, *, usuario=None, url_origem=""):
    """Veredito de UM link já afiliado: aprovado, reprovado ou incerto."""
    from apps.scrapers.marketplaces.registry import get_marketplace

    slug = _marketplace_da_url(url_origem or url_afiliado)
    if not slug:
        # Loja fora do nosso conjunto: não sabemos verificar, então não publicamos.
        # Silenciosamente deixar passar seria exatamente o buraco que este módulo fecha.
        return REPROVADO, "Loja não reconhecida; não há como conferir o destino."

    loja = get_marketplace(slug)
    try:
        relatorio = loja.verify_link(url_afiliado, usuario=usuario)
    except Exception as exc:
        # Qualquer falha de transporte é INCERTO, nunca reprovação (ver docstring).
        logger.info("Verificação de link de canal ficou incerta (%s): %s",
                    type(exc).__name__, exc)
        return INCERTO, "Não foi possível conferir o destino agora."

    if not isinstance(relatorio, dict):
        return INCERTO, "Verificador não devolveu um veredito legível."
    if relatorio.get("ok"):
        return APROVADO, ""
    motivo = str(relatorio.get("motivo") or "").strip()
    if relatorio.get("transitorio") or relatorio.get("incerto"):
        return INCERTO, motivo or "Destino não respondeu de forma conclusiva."
    return REPROVADO, motivo or "O destino não foi aprovado."


def mensagem_liberada(links, *, usuario=None):
    """A mensagem inteira pode sair?

    ``links`` é uma lista de (url_origem, url_afiliado). Devolve
    ``(liberada, veredito, motivo)``. ``veredito`` vale APROVADO, REPROVADO ou
    INCERTO e é o pior encontrado — porque a mensagem sai inteira ou não sai.
    """
    if not links:
        return False, REPROVADO, "Nenhum link de produto para conferir."

    pior = APROVADO
    motivo_final = ""
    for url_origem, url_afiliado in links:
        veredito, motivo = verificar_link(
            url_afiliado, usuario=usuario, url_origem=url_origem,
        )
        if veredito == REPROVADO:
            # Reprovação é definitiva e encerra a checagem: não adianta conferir o
            # resto se a mensagem já não pode sair.
            return False, REPROVADO, motivo
        if veredito == INCERTO:
            pior, motivo_final = INCERTO, motivo
    if pior == INCERTO:
        return False, INCERTO, motivo_final or "Destino não confirmado; tenta de novo."
    return True, APROVADO, ""
