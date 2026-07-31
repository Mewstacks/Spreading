from datetime import timedelta
from django.utils import timezone


def expire_stale(max_age_hours=48):
    """Expiração gradual; não remove linhas nem histórico."""
    from apps.scrapers.models import Produto, CupomNormalizado, ProdutoCupom
    now = timezone.now()
    cutoff = now - timedelta(hours=max_age_hours)
    stale_products = Produto.objects.filter(
        ultima_observacao__lt=cutoff, estado="ativo"
    ).update(estado="stale", falha_verificacao="Fonte sem confirmar a oferta há 48h")
    expired_coupons = CupomNormalizado.objects.filter(
        validade__lt=now, estado="ativo"
    ).update(estado="expirado")
    ProdutoCupom.objects.filter(cupom__estado="expirado").exclude(
        status="expirado").update(status="expirado")
    return {"products": stale_products, "coupons": expired_coupons}


def purgar_eventos_antigos(dias=30):
    """Apaga EventoOperacional velho. Sem isto a tabela de log cresce para sempre.

    30 dias porque o relatório de saúde olha no máximo 7 e o resto serve para
    comparar com o mês anterior; guardar mais só paga armazenamento para responder
    pergunta que ninguém faz. Erros que importam viram correção no código, não
    arquivo histórico.
    """
    from apps.scrapers.models import EventoOperacional
    cutoff = timezone.now() - timedelta(days=dias)
    apagados, _ = EventoOperacional.objects.filter(criado_em__lt=cutoff).delete()
    return apagados


def reconciliar_publicacoes_orfas(max_age_minutes=30):
    """Fecha Publicacao presas em 'pendente' — o worker morreu no meio do envio.

    A linha nasce 'pendente' antes do trabalho e todo erro previsto já a marca
    'falhou'; sobra o processo morto (deploy/crash) entre o create e o desfecho, que
    nenhum except captura. Um envio real leva no máximo dezenas de segundos (Playwright
    do ML), então 'pendente' há 30min não é um envio em curso — é uma órfã.
    """
    from apps.scrapers.models import Publicacao
    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    return Publicacao.objects.filter(status="pendente", criada_em__lt=cutoff).update(
        status="falhou", erro="Envio interrompido antes de concluir (worker reiniciado).")
