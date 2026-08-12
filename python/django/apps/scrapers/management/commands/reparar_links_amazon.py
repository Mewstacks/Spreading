"""Audita e repara links Amazon invalidados pelo verificador do Mercado Livre.

Contexto: até a correção deste ciclo, `verificar_links_pendentes` do Mercado Livre
varria `LinkAfiliadoUsuario` sem recorte de loja. Todo link da Amazon caía naquela
fila, era aberto no Chromium e reprovado com "O link não abre uma página de produto
do Mercado Livre" — em produção, 47 links válidos. Alguns acumularam tentativas até
`nao_afiliavel`, o estado terminal, e nunca mais voltariam sozinhos.

O reparo é IDEMPOTENTE e local: reconstrói o link canônico pelo ASIN e pela tag do
próprio usuário e aprova quando os dois batem. Reprovação legítima da Amazon
(anúncio indisponível) é preservada — ver `_MOTIVOS_LEGITIMOS` em
`scraper_amazon/link.py`.

    python manage.py reparar_links_amazon --dry-run   # só audita
    python manage.py reparar_links_amazon             # audita e repara
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.accounts.tenant import system_job
from apps.scrapers.models import LinkAfiliadoUsuario


def auditar_usuario(usuario) -> dict:
    """Conta, sem escrever nada, o que o reparo faria para este usuário."""
    from apps.scrapers.scraper_amazon.link import (
        _MOTIVOS_LEGITIMOS, _tag, link_coerente,
    )

    linhas = list(
        LinkAfiliadoUsuario.objects
        .filter(usuario=usuario, produto__marketplace="amazon")
        .filter(Q(verificado_ok__isnull=True) | Q(verificado_ok=False))
        .select_related("produto")
    )
    resumo = {
        "pendentes": len(linhas), "sem_tag": 0, "reparaveis": 0,
        "regerar": 0, "legitimos": 0, "terminais": 0,
    }
    if not linhas:
        return resumo
    if not _tag(usuario):
        # Um único bloqueio de conta: nenhum destes itens tem defeito próprio.
        resumo["sem_tag"] = len(linhas)
        return resumo
    for linha in linhas:
        motivo = (linha.verificacao_motivo or "").casefold()
        if linha.verificado_ok is False and any(m in motivo for m in _MOTIVOS_LEGITIMOS):
            resumo["legitimos"] += 1
            continue
        if linha.estado == "nao_afiliavel":
            resumo["terminais"] += 1
        if link_coerente(linha.link_afiliado, linha.produto, usuario=usuario):
            resumo["reparaveis"] += 1
        else:
            resumo["regerar"] += 1
    return resumo


class Command(BaseCommand):
    help = ("Audita (e opcionalmente repara) links Amazon reprovados "
            "indevidamente pelo verificador do Mercado Livre.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Só audita; não escreve nada no banco.")
        parser.add_argument("--limite", type=int, default=5000,
                            help="Máximo de links por usuário em um reparo.")

    @system_job
    def handle(self, *args, **opts):
        from apps.scrapers.scraper_amazon.link import verificar_links_pendentes

        total = {"pendentes": 0, "sem_tag": 0, "reparaveis": 0, "regerar": 0,
                 "legitimos": 0, "terminais": 0}
        reparados = {"aprovados": 0, "regerados": 0, "reprovados": 0,
                     "transitorios": 0, "bloqueados": 0}
        for usuario in get_user_model().objects.filter(is_active=True):
            resumo = auditar_usuario(usuario)
            for chave in total:
                total[chave] += resumo[chave]
            if any(resumo[c] for c in ("reparaveis", "regerar", "terminais")):
                self.stdout.write(
                    f"{usuario.get_username()}: {resumo['pendentes']} pendente(s), "
                    f"{resumo['reparaveis']} aprovável(is) localmente, "
                    f"{resumo['regerar']} a regerar, "
                    f"{resumo['terminais']} em estado terminal, "
                    f"{resumo['legitimos']} reprovação(ões) legítima(s)."
                )
            if resumo["sem_tag"]:
                self.stdout.write(
                    f"{usuario.get_username()}: {resumo['sem_tag']} link(s) "
                    "bloqueado(s) por tag Amazon ausente (problema de conta, "
                    "não dos produtos)."
                )
            if opts["dry_run"]:
                continue
            resultado = verificar_links_pendentes(
                usuario, limite=opts["limite"], incluir_reprovados=True,
            )
            for chave in reparados:
                reparados[chave] += int(resultado.get(chave, 0) or 0)

        self.stdout.write(
            f"AUDITORIA: {total['pendentes']} link(s) Amazon sem veredito válido; "
            f"{total['reparaveis']} aprovável(is), {total['regerar']} a regerar, "
            f"{total['terminais']} terminal(is), {total['legitimos']} legítimo(s), "
            f"{total['sem_tag']} bloqueado(s) por conta."
        )
        if opts["dry_run"]:
            self.stdout.write("Execução seca: nada foi alterado.")
            return
        self.stdout.write(
            f"REPARO: {reparados['aprovados']} aprovado(s) "
            f"({reparados['regerados']} com link refeito), "
            f"{reparados['reprovados']} reprovação(ões) preservada(s), "
            f"{reparados['transitorios']} sem link possível, "
            f"{reparados['bloqueados']} bloqueado(s) por conta."
        )
