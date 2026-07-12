"""Worker que lê canais curados (Telegram) e re-divulga com a tag do dono (B4).

Usa Telethon (userbot MTProto): uma CONTA de usuário entra nos canais-fonte e lê as
mensagens novas. Para cada mensagem com link de produto, troca a URL pela versão
afiliada do dono do CanalMonitorado e envia ao grupo de destino (WhatsApp/Telegram),
com dedup por URL-fonte (EnvioCanal).

Requer settings.TELEGRAM_API_ID/API_HASH/SESSION. Sem eles, o worker fica ocioso.
Rode:  python manage.py monitorar_canais --tick 60
"""
import logging
import time
import traceback

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Lê canais curados no Telegram e re-divulga com a tag de afiliado do dono."

    def add_arguments(self, parser):
        parser.add_argument("--tick", type=int, default=60,
                            help="Segundos entre varreduras dos canais.")

    def handle(self, *args, **opts):
        if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH
                and settings.TELEGRAM_SESSION):
            logger.info("Telegram userbot nao configurado; worker ocioso")
            # Fica vivo mas ocioso (honcho reinicia se sair); evita crash-loop.
            while True:
                time.sleep(300)

        # Import tardio: só exige telethon quando de fato configurado.
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession

        tick = max(10, opts["tick"])
        logger.info("Worker de canais no ar; varre a cada %ss", tick)
        client = TelegramClient(
            StringSession(settings.TELEGRAM_SESSION),
            settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH,
        )
        client.start()
        try:
            while True:
                try:
                    self._varrer(client)
                except Exception:
                    logger.error("Erro na varredura de canais:\n%s", traceback.format_exc())
                time.sleep(tick)
        finally:
            client.disconnect()

    def _varrer(self, client):
        from apps.scrapers.models import CanalMonitorado, EnvioCanal
        from apps.scrapers.canais.relink import reescrever_mensagem
        from apps.scrapers.senders.registry import get_sender

        for canal in CanalMonitorado.objects.filter(ativo=True).select_related("owner"):
            try:
                self._processar_canal(client, canal, EnvioCanal, reescrever_mensagem, get_sender)
            except Exception as e:
                logger.warning("Falha no canal %s: %s", canal.handle, e)

    def _processar_canal(self, client, canal, EnvioCanal, reescrever_mensagem, get_sender):
        sender = get_sender(canal.destino_canal)
        maior_id = canal.ultimo_id
        # reverse=True: da mais antiga p/ a mais nova entre as não vistas (min_id).
        for msg in client.iter_messages(canal.handle, min_id=canal.ultimo_id,
                                        reverse=True, limit=50):
            maior_id = max(maior_id, msg.id)
            texto = msg.message or ""
            if not texto:
                continue
            novo_texto, chaves = reescrever_mensagem(texto, canal.owner)
            if not chaves:
                continue  # nenhuma URL de produto re-linkada
            # Dedup: já divulgou alguma dessas ofertas p/ este dono?
            ja = set(EnvioCanal.objects.filter(owner=canal.owner, chave__in=chaves)
                     .values_list("chave", flat=True))
            novas = [c for c in chaves if c not in ja]
            if not novas:
                continue
            resultado = sender.enviar_oferta(canal.destino_grupo_id, novo_texto,
                                             legenda=novo_texto)
            if resultado.get("sucesso"):
                EnvioCanal.objects.bulk_create(
                    [EnvioCanal(owner=canal.owner, chave=c) for c in novas],
                    ignore_conflicts=True,
                )
                logger.info("Canal %s -> %s divulgado", canal.handle, canal.destino_grupo_id)
            else:
                logger.warning("Falha no envio de canal: %s", resultado.get("erro"))
        # Avança o cursor mesmo sem envio (não reprocessa msgs antigas no restart).
        if maior_id > canal.ultimo_id:
            canal.ultimo_id = maior_id
            canal.save(update_fields=["ultimo_id"])
