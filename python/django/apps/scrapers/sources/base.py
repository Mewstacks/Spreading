import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

_DECIMAL_COM_PONTO = re.compile(r"^\d+\.\d{1,2}$")


def normalizar_dinheiro(texto) -> float:
    """Converte um preço renderizado em float, sem confundir milhar com decimal.

    A Amazon alterna entre "R$ 1.299,90" (ponto de milhar) e "R$ 64.99" (ponto
    decimal, quando a página cai no formato en-US). Tratar todo ponto como milhar
    multiplicava o segundo caso por 100 e o valor errado passava por todos os
    gates de desconto.
    """
    bruto = str(texto or "").replace("\xa0", " ").replace("R$", "")
    bruto = re.sub(r"[^\d.,]", "", bruto)
    if not bruto:
        return 0.0
    if "," in bruto:
        # Vírgula presente => ela é o decimal e o ponto é separador de milhar.
        bruto = bruto.replace(".", "").replace(",", ".")
    elif not _DECIMAL_COM_PONTO.match(bruto):
        # Sem vírgula: só é decimal quando há 1-2 dígitos após um único ponto.
        bruto = bruto.replace(".", "")
    try:
        return round(float(bruto), 2)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class IngestedItem:
    external_id: str
    marketplace: str
    source: str
    kind: str
    canonical_url: str
    title: str
    current_price: float = 0
    # Preço realmente pago quando a fonte conhece um desconto ativável além da
    # vitrine (cupom oficial Amazon ou badge "com Cupom" do ML).
    # Vazio significa "igual ao current".
    effective_price: float = 0
    reference_price: float = 0
    image_url: str = ""
    # Subcategoria conforme a PRÓPRIA loja classificou (browse node da Amazon,
    # domain_id do ML). Vazio = "esta fonte não sabe", que é o caso das fontes
    # públicas: a busca e a página de ofertas não expõem o nó, e descobri-lo
    # custaria uma carga de PDP por item. Vazio nunca sobrescreve uma categoria
    # real já gravada por outra fonte — ver persist_items.
    category: str = ""
    coupon_code: str = ""
    coupon_rules: dict[str, Any] = field(default_factory=dict)
    content_type: str = "voucher"
    starts_at: datetime | None = None
    restricted: bool = False
    flash: bool = False
    valid_until: datetime | None = None
    observed_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class SourceAdapter:
    slug = ""
    marketplace = ""
    name = ""
    # A fonte é capaz de afirmar "vi o inventário inteiro"?
    #
    # A página oficial de cupons de uma loja é: ou lista todos, ou está quebrada.
    # Uma vitrine curada de terceiro (Promobit) e a prévia das 20 mensagens mais
    # recentes de um canal NÃO são — mostram um recorte por construção, e ausência
    # ali nunca prova que o item acabou.
    #
    # Não é detalhe de relatório: `maintenance.diagnosticar_alertas_pipeline_cupons`
    # acusa fonte que passa dois ciclos sem se declarar completa, e uma fonte
    # inerentemente parcial dispararia esse alerta para sempre — ruído permanente no
    # lugar exato onde o operador precisa enxergar problema de verdade.
    inventario_completo = True

    def discover_offers(self, **kwargs) -> Iterable[IngestedItem]:
        return []

    def discover_coupons(self, **kwargs) -> Iterable[IngestedItem]:
        return []

    def refresh_offer(self, item: IngestedItem, **kwargs) -> IngestedItem | None:
        raise NotImplementedError

    def healthcheck(self) -> dict:
        return {"ok": True}
