"""Diagnóstico READ-ONLY do pipeline (catálogo, Amazon, cupons e envios).

Existe porque produção roda com RLS: sem `system_context` uma consulta de suporte
enxerga zero linha e o diagnóstico mente. Este comando NÃO escreve nada — nenhum
save/update/create/delete — para poder ser rodado em produção durante incidente.

`TENANT_SYSTEM_PROCESS=1` é OBRIGATÓRIO e não é decoração: é ele que faz o settings
escolher SYSTEM_DATABASE_URL (role `spreading_system`) em vez da DATABASE_URL da web.
`system_context` verifica a role do banco e recusa qualquer outra, então sem o env var
o comando morre em PermissionDenied ("A role de banco desta máquina não pode abrir
contexto cross-tenant") antes de imprimir uma linha. Mesmo prefixo que o Procfile usa
em todos os workers cross-tenant.

    fly ssh console -a spreading-web -C \
        "env TENANT_SYSTEM_PROCESS=1 python3 /app/django/manage.py diagnostico_producao"

`--secao` limita a saída (catalogo, amazon, cupons, envios, eventos).
"""
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count, ExpressionWrapper, F, FloatField, Max, Q
from django.utils import timezone

from apps.accounts.tenant import system_context

SECOES = ("catalogo", "amazon", "cupons", "envios", "conexoes", "eventos")


class Command(BaseCommand):
    help = "Diagnóstico read-only do pipeline de ofertas, cupons e envios."

    def add_arguments(self, parser):
        parser.add_argument("--secao", choices=SECOES, action="append", default=None,
                            help="Limita a saída (repetível). Padrão: todas.")
        parser.add_argument("--dias", type=int, default=7,
                            help="Janela de publicações/eventos analisada.")

    def handle(self, *args, **opts):
        secoes = set(opts["secao"] or SECOES)
        dias = max(1, opts["dias"])
        with system_context():
            self._executar(secoes, dias)

    # ── infra de saída ────────────────────────────────────────────────────
    def _sec(self, titulo):
        self.stdout.write("\n" + "=" * 10 + f" {titulo} " + "=" * 10)

    def _linha(self, texto=""):
        self.stdout.write(str(texto))

    @staticmethod
    def _motivo_reprovacao(cupom) -> str:
        """Primeiro portão que este cupom não passou, na ordem em que são aplicados.

        Sem isto o funil só diz "1665 reprovados" e não dá para saber se falta
        ligar uma flag, se a fonte parou de trazer um campo, ou se os cupons
        simplesmente não são publicáveis.
        """
        from django.conf import settings
        from apps.scrapers.coupon_rules import _FONTES_ML_ATIVACAO, regras_do_cupom

        regras = regras_do_cupom(cupom)
        if regras["modo_resgate"] == "codigo":
            return "modo=codigo mas o código não é humano/digitável"
        marketplace = str(getattr(cupom, "marketplace", "") or "").casefold()
        if marketplace == "amazon":
            evidencia = getattr(cupom, "evidencia", {}) or {}
            if not evidencia.get("promotion_id"):
                return "amazon: evidência sem promotion_id"
            if not evidencia.get("asins"):
                return "amazon: evidência sem ASINs"
            return "amazon: associação não é da página oficial"
        if marketplace != "mercadolivre":
            return f"marketplace {marketplace!r} não tem regra de ativação"
        if getattr(getattr(cupom, "fonte", None), "slug", "") not in _FONTES_ML_ATIVACAO:
            return "fonte ML fora da lista de ativação"
        if not str(getattr(cupom, "external_id", "") or "").startswith("campanha:"):
            return "external_id não é campanha:"
        if regras.get("is_mar_aberto"):
            return "campanha site-wide (mar aberto)"
        from apps.scrapers.coupon_rules import listagem_publica_ml
        if not listagem_publica_ml(cupom):
            return "campanha sem listagem pública (container_url nem link de lista)"
        if regras.get("valor_desconto") in (None, "", 0):
            return "campanha sem valor de desconto"
        if not settings.ML_CUPONS_ATIVACAO_ENABLED:
            return "APTO — só falta ML_CUPONS_ATIVACAO_ENABLED"
        return "flag ligada globalmente, mas não para esta organização"

    # ── diagnóstico ───────────────────────────────────────────────────────
    def _executar(self, secoes, dias):
        from apps.scrapers.maintenance import cupons_frescos_q, produtos_frescos_q
        from apps.scrapers.models import (
            ConfiguracaoEnvio, CupomNormalizado, CupomPreparacao, EventoOperacional,
            FonteIngestao, HistoricoEnvio, LinkAfiliadoCupomUsuario,
            LinkAfiliadoUsuario, Produto, ProdutoCupom, Publicacao,
        )
        from apps.accounts.models import Perfil

        agora = timezone.now()
        desde = agora - timezone.timedelta(days=dias)
        usuarios = list(get_user_model().objects.filter(is_active=True).order_by("id"))
        self._linha(f"AGORA={agora.isoformat()}  janela={dias}d  usuarios_ativos={len(usuarios)}")

        if "catalogo" in secoes:
            self._sec("USUARIOS")
            for u in usuarios:
                p = Perfil.objects.filter(user=u).first()
                self._linha(
                    f"  id={u.id} {u.get_username()!r} staff={u.is_staff} "
                    f"tag_az={bool(getattr(p, 'afiliado_tag_amazon', ''))} "
                    f"cred_az={bool(getattr(p, 'amazon_credential_id', ''))} "
                    f"sec_az={bool(getattr(p, 'amazon_credential_secret', ''))} "
                    f"elegivel={getattr(p, 'amazon_elegivel', None)} "
                    f"bloq={getattr(p, 'bloqueado', None)} "
                    f"erro_az={(getattr(p, 'amazon_ultimo_erro', '') or '')[:60]!r}")

            self._sec("PRODUTOS")
            self._linha(f"  total={Produto.objects.count()}")
            self._linha("  por marketplace: " + str(list(
                Produto.objects.values("marketplace").annotate(n=Count("id")).order_by("-n"))))
            self._linha("  FRESCOS (<48h) por marketplace: " + str(list(
                Produto.objects.filter(produtos_frescos_q())
                .values("marketplace").annotate(n=Count("id")).order_by("-n"))))
            self._linha("  por marketplace/estado: " + str(list(
                Produto.objects.values("marketplace", "estado")
                .annotate(n=Count("id")).order_by("-n")[:15])))
            self._linha("  por fonte (top 15):")
            for r in (Produto.objects.values("marketplace", "fonte")
                      .annotate(n=Count("id"), ult=Max("ultima_observacao"))
                      .order_by("-n")[:15]):
                self._linha(f"    {r['marketplace']:14} {r['fonte'][:28]:28} n={r['n']:6} ult={r['ult']}")

            self._sec("FONTES DE INGESTAO")
            for f in FonteIngestao.objects.all().order_by("marketplace", "slug"):
                self._linha(
                    f"  {f.marketplace[:12]:12} {f.slug[:30]:30} hab={f.habilitada} "
                    f"status={f.status:9} ultimo_total={f.ultimo_total:5} "
                    f"falhas={f.falhas_consecutivas} sucesso={f.ultimo_sucesso} "
                    f"tent={f.ultima_tentativa} erro={(f.erro_publico or '')[:55]!r}")

        if "amazon" in secoes:
            self._sec("AMAZON")
            az = Produto.objects.filter(marketplace="amazon")
            self._linha(f"  total={az.count()} ultima_observacao_max={az.aggregate(m=Max('ultima_observacao'))['m']}")
            self._linha("  por origem: " + str(list(az.values("origem").annotate(n=Count("id")))))
            self._linha("  por fonte: " + str(list(az.values("fonte").annotate(n=Count("id")))))
            self._linha("  por estado: " + str(list(az.values("estado").annotate(n=Count("id")))))
            self._linha("  por owner: " + str(list(az.values("owner").annotate(n=Count("id")))))
            self._linha(f"  frescos={az.filter(produtos_frescos_q()).count()}")
            self._linha(f"  com categoria real={az.exclude(categoria='DESCONHECIDO').exclude(categoria='').count()}")
            self._linha("  por macro: " + str(list(
                az.values("macro_categoria").annotate(n=Count("id")).order_by("-n")[:10])))
            self._linha(f"  contendo 'aspirador'={az.filter(nome__icontains='aspirador').count()}")
            for p in az.filter(nome__icontains="aspirador").order_by("-ultima_observacao")[:5]:
                self._linha(f"    {p.asin} {p.estado:9} de={p.preco_sem_desconto} por={p.preco_com_cupom} "
                            f"obs={p.ultima_observacao:%m-%d %H:%M} {p.nome[:45]}")

            self._sec("VITRINE /scrapers/top/ (mesma query da tela)")
            from apps.scrapers.ofertas import anotacao_preco_publicado
            for u in usuarios:
                qs = (Produto.objects
                      .filter(produtos_frescos_q(), preco_sem_desconto__gt=0)
                      .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
                      .filter(Q(owner__isnull=True) | Q(owner=u))
                      .annotate(preco_publicado=anotacao_preco_publicado())
                      .annotate(percent=ExpressionWrapper(
                          (F("preco_sem_desconto") - F("preco_publicado")) * 100.0
                          / F("preco_sem_desconto"), output_field=FloatField()))
                      .filter(percent__gt=0, percent__lt=90))
                por_loja = list(qs.values("marketplace").annotate(n=Count("id")).order_by("-n"))
                if not any(r["n"] for r in por_loja):
                    continue
                melhor_az = qs.filter(marketplace="amazon").order_by("-percent").first()
                acima = (qs.filter(marketplace="mercadolivre",
                                   percent__gte=getattr(melhor_az, "percent", 0)).count()
                         if melhor_az else None)
                self._linha(f"  user {u.id}: total={qs.count()} {por_loja}")
                if melhor_az:
                    self._linha(f"    melhor Amazon={melhor_az.percent:.1f}% -> {acima} itens ML acima dele "
                                f"(pagina ~{acima // 20 + 1} de 20/pag)")

        if "cupons" in secoes:
            self._sec("CUPONS — catalogo")
            from apps.scrapers.coupon_products import mapa_relacoes_prontas
            from apps.scrapers.coupon_rules import ativacao_publicavel, codigo_publicavel, cupom_publicavel
            self._linha(f"  total={CupomNormalizado.objects.count()}")
            self._linha("  por estado: " + str(list(
                CupomNormalizado.objects.values("estado").annotate(n=Count("id")).order_by("-n"))))
            ativos = CupomNormalizado.objects.filter(estado="ativo")
            frescos = ativos.filter(cupons_frescos_q())
            self._linha(f"  ativos={ativos.count()} ativos+frescos={frescos.count()}")
            self._linha("  frescos por fonte: " + str(list(
                frescos.values("fonte__slug").annotate(n=Count("id")).order_by("-n")[:15])))
            self._linha("  frescos por marketplace: " + str(list(
                frescos.values("marketplace").annotate(n=Count("id")).order_by("-n"))))
            self._linha(f"  com codigo={frescos.exclude(codigo='').count()} "
                        f"sem codigo={frescos.filter(codigo='').count()}")

            self._sec("CUPONS — funil de publicabilidade")
            lista = list(frescos.select_related("fonte", "integracao", "programa"))
            com_codigo = [c for c in lista if codigo_publicavel(c)]
            por_ativacao = [c for c in lista if ativacao_publicavel(c)]
            publicaveis = [c for c in lista if cupom_publicavel(c)]
            self._linha(f"  frescos={len(lista)} -> codigo_publicavel={len(com_codigo)} "
                        f"ativacao_publicavel={len(por_ativacao)} PUBLICAVEIS={len(publicaveis)}")
            reprovados = [c for c in lista if c not in publicaveis]
            if reprovados:
                from collections import Counter
                self._linha("  reprovados por fonte: " + str(
                    Counter(getattr(c.fonte, "slug", "?") for c in reprovados).most_common(10)))
                self._linha("  POR QUE cada um foi reprovado:")
                self._linha("  " + str(Counter(
                    self._motivo_reprovacao(c) for c in reprovados).most_common(12)))
                # Esta sonda já cumpriu o papel: em produção mostrou que os 2062
                # reprovados tinham `link` em lista.mercadolivre.com.br — listagem
                # pública que o preparo (`_coletar_ml_remoto`) já aceitava. O portão
                # passou a aceitá-la (`coupon_rules.listagem_publica_ml`). Fica no ar
                # para o caso de a fonte voltar a mudar a cara desses links.
                sem_container = [c for c in reprovados
                                 if "listagem pública" in self._motivo_reprovacao(c)]
                if sem_container:
                    hosts = Counter()
                    for c in sem_container:
                        try:
                            hosts[urlsplit(c.link or "").netloc.casefold()] += 1
                        except ValueError:
                            hosts["<url inválida>"] += 1
                    self._linha("  host do `link` desses cupons: "
                                + str(hosts.most_common(6)))
                    for c in sem_container[:4]:
                        self._linha(f"    exemplo: {(c.link or '')[:110]}")

            self._sec("CUPONS — prontidao por usuario")
            self._linha("  CupomPreparacao por status: " + str(list(
                CupomPreparacao.objects.values("status").annotate(n=Count("id")).order_by("-n"))))
            self._linha("  ProdutoCupom por status: " + str(list(
                ProdutoCupom.objects.values("status").annotate(n=Count("id")).order_by("-n"))))
            self._linha(f"  LinkAfiliadoCupomUsuario={LinkAfiliadoCupomUsuario.objects.count()}")
            self._linha("  LinkAfiliadoUsuario por estado: " + str(list(
                LinkAfiliadoUsuario.objects.values("estado").annotate(n=Count("id")).order_by("-n"))))
            self._linha("  LinkAfiliadoUsuario verificado_ok: " + str(list(
                LinkAfiliadoUsuario.objects.values("verificado_ok").annotate(n=Count("id")))))
            for u in usuarios:
                visiveis = [c for c in publicaveis if c.owner_id in (None, u.id)]
                if not visiveis:
                    continue
                preparadas, prontas = mapa_relacoes_prontas(u, visiveis)
                self._linha(f"  user {u.id}: publicaveis_visiveis={len(visiveis)} "
                            f"preparados={len(preparadas)} PRONTOS={len(prontas)}")

        if "envios" in secoes:
            self._sec("REGRAS DE ENVIO")
            configs = list(ConfiguracaoEnvio.objects.all().select_related("owner").order_by("id"))
            self._linha(f"  total={len(configs)} ativas={sum(1 for c in configs if c.ativo)}")
            for c in configs:
                self._linha(
                    f"  id={c.id} owner={c.owner_id} ativo={c.ativo} canal={c.canal} "
                    f"mkt={c.marketplace!r} macro={c.macro_categoria!r} termo={(c.termo_busca or '')[:32]!r}")
                self._linha(
                    f"     destino={c.grupo_nome[:28]!r} intervalo={c.intervalo_minutos}min "
                    f"janela={c.janela_inicio}-{c.janela_fim}h min_desc={c.min_desconto_percent} "
                    f"max_dia={c.max_envios_dia} restritos={c.incluir_restritos} "
                    f"sem_desconto={c.incluir_sem_desconto}")
                self._linha(
                    f"     ultimo_envio={c.ultimo_envio} proximo={c.proximo_envio} "
                    f"na_janela={c.dentro_da_janela(agora)} "
                    f"vencida={c.proximo_envio is None or agora >= c.proximo_envio} "
                    f"falhas={c.falhas_consecutivas} pausa={c.motivo_pausa[:50]!r}")

            self._sec("POOL DE CONTEUDO por regra ativa (produto vs cupom)")
            from apps.scrapers.content_ranking import (
                _coupon_candidates, _product_candidates, selecionar_conteudo_para_grupo,
            )
            for c in configs:
                if not c.ativo:
                    continue
                try:
                    produtos = _product_candidates(c, 8)
                    cupons_cand = _coupon_candidates(c, 8)
                    efetivos = selecionar_conteudo_para_grupo(c, 8)
                except Exception as exc:  # diagnóstico não pode derrubar o resto
                    self._linha(f"  config {c.id}: ERRO ao montar pool: {type(exc).__name__}: {exc}")
                    continue
                self._linha(f"  config {c.id}: produtos={len(produtos)} cupons={len(cupons_cand)}")
                for cand in efetivos[:5]:
                    titulo = getattr(cand.obj, "titulo", "") or getattr(cand.obj, "nome", "")
                    self._linha(f"     {cand.kind:7} score={cand.score:6.2f} {titulo[:55]!r}")

            self._sec(f"PUBLICACOES ({dias}d)")
            pubs = Publicacao.objects.filter(criada_em__gte=desde)
            self._linha("  por status: " + str(list(
                pubs.values("status").annotate(n=Count("id")).order_by("-n"))))
            self._linha("  por origem/status: " + str(list(
                pubs.values("origem", "status").annotate(n=Count("id")).order_by("-n")[:12])))
            self._linha("  ultimas 15:")
            for p in pubs.order_by("-criada_em")[:15]:
                self._linha(f"    {p.criada_em:%m-%d %H:%M} {p.origem:8} {p.status:9} "
                            f"user={p.usuario_id} cupom={p.cupom_normalizado_id} "
                            f"prod={p.produto_id} erro={(p.erro or '')[:60]!r}")
            self._linha(f"  HistoricoEnvio total={HistoricoEnvio.objects.count()} "
                        f"janela={HistoricoEnvio.objects.filter(data_envio__gte=desde).count()}")

        if "conexoes" in secoes:
            # Sem segredos: este bloco existe para diferenciar "pipeline vazio" de
            # "estoque pronto, mas a conta de teste nao autenticou o destino/loja".
            # Antes era preciso montar um shell ad-hoc em producao, sujeito a vazar
            # token por engano justamente durante o diagnostico de um incidente.
            from apps.accounts.models import (
                BrowserSession, MercadoLivreSession, WhatsAppConnection,
            )
            from apps.scrapers.models import IntegracaoAfiliado

            self._sec("CONEXOES POR USUARIO (sem segredos)")
            for u in usuarios:
                perfil = Perfil.objects.filter(user=u).first()
                org_id = getattr(perfil, "active_organization_id", None)
                wa = WhatsAppConnection.objects.filter(organization_id=org_id).first()
                ml = MercadoLivreSession.objects.filter(organization_id=org_id).first()
                browser = list(
                    BrowserSession.objects.filter(user=u, organization_id=org_id)
                    .values("provider", "status", "probe_result", "probe_reason",
                            "last_probe_at", "last_used_at")
                )
                afiliados = list(
                    IntegracaoAfiliado.objects.filter(owner=u)
                    .values("provedor", "status", "habilitada", "erro_publico",
                            "ultimo_sucesso")
                )
                self._linha(
                    f"  user={u.id} {u.get_username()!r} org={org_id} "
                    f"amazon_tag={bool(getattr(perfil, 'afiliado_tag_amazon', ''))} "
                    f"amazon_creators={bool(getattr(perfil, 'amazon_credential_id', '')) and bool(getattr(perfil, 'amazon_credential_secret', ''))} "
                    f"amazon_elegivel={getattr(perfil, 'amazon_elegivel', None)} "
                    f"telegram_destino={bool(getattr(perfil, 'telegram_bot_token', ''))}"
                )
                self._linha(
                    "     whatsapp=" + (str({
                        "status": wa.status, "phase": wa.phase,
                        "last_event": wa.last_event, "last_event_at": wa.last_event_at,
                        "unavailable_reason": wa.unavailable_reason,
                        "consistency": wa.consistency_status,
                    }) if wa else "ausente")
                )
                self._linha(
                    "     mercado_livre=" + (str({
                        "status": ml.status, "probe": ml.last_probe_result,
                        "probe_reason": ml.probe_reason,
                        "linkbuilder": ml.lb_readiness,
                        "linkbuilder_reason": ml.lb_readiness_reason,
                        "last_used_at": ml.last_used_at,
                    }) if ml else "ausente")
                )
                self._linha(f"     browser_sessions={browser or 'nenhuma'}")
                self._linha(f"     integracoes_afiliado={afiliados or 'nenhuma'}")

        if "eventos" in secoes:
            self._sec(f"EVENTOS ({dias}d) — contagem")
            self._linha("  " + str(list(
                EventoOperacional.objects.filter(criado_em__gte=desde)
                .values("pipeline", "evento", "level").annotate(n=Count("id"))
                .order_by("-n")[:25])))
            self._sec(f"EVENTOS ({dias}d) — ultimos error/warning")
            for e in (EventoOperacional.objects
                      .filter(criado_em__gte=desde, level__in=("error", "warning"))
                      .order_by("-criado_em")[:40]):
                self._linha(f"  {e.criado_em:%m-%d %H:%M} [{e.level:7}] {e.pipeline}/{e.evento} "
                            f"user={e.usuario_id} :: {(e.mensagem or '')[:100]}")
