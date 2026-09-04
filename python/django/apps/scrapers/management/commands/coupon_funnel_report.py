from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.utils import timezone

from apps.scrapers.models import (
    CupomDisponibilidade, CupomFonteObservacao, CupomPreparacao,
    ExecucaoIngestao,
    LinkAfiliadoCupomUsuario, LinkAfiliadoProdutoCupomUsuario, ProdutoCupom,
    Publicacao,
)


class Command(BaseCommand):
    help = "Relatório reconciliável do funil de cupons por loja, fonte e modo."

    def add_arguments(self, parser):
        parser.add_argument("--stale-minutes", type=int, default=20)
        parser.add_argument("--channel", default="whatsapp")

    def handle(self, *args, **options):
        from apps.accounts.tenant import system_context
        from apps.scrapers.maintenance import (
            cupons_frescos_q, diagnosticar_alertas_pipeline_cupons,
        )

        with system_context():
            self._report(options, cupons_frescos_q, diagnosticar_alertas_pipeline_cupons)

    def _report(self, options, cupons_frescos_q, diagnosticar_alertas_pipeline_cupons):
        now = timezone.now()
        projections = CupomDisponibilidade.objects.filter(
            channel=options["channel"],
        )
        visible_ready = projections.filter(
            stage="ready", cupom__estado="ativo",
        ).filter(cupons_frescos_q(agora=now, prefix="cupom__"))
        funnel = {
            "observed": CupomFonteObservacao.objects.count(),
            "accepted": CupomFonteObservacao.objects.filter(outcome="accepted").count(),
            "eligible": projections.filter(
                stage__in=("eligible", "prepared", "waiting_link", "ready"),
            ).count(),
            "associated": ProdutoCupom.objects.filter(status="confirmado").count(),
            "prepared": CupomPreparacao.objects.filter(status="pronto").count(),
            "link_verified": (
                LinkAfiliadoCupomUsuario.objects.filter(verificado_ok=True).count()
                + LinkAfiliadoProdutoCupomUsuario.objects.filter(verificado_ok=True).count()
            ),
            "ready": projections.filter(stage="ready").count(),
            # A tela aplica exatamente ativo + frescor sobre a projeção ready.
            "listed": visible_ready.count(),
            "sent": Publicacao.objects.filter(
                cupom_normalizado__isnull=False, status="enviado",
            ).count(),
        }
        self.stdout.write("FUNNEL\t" + "\t".join(
            f"{stage}={total}" for stage, total in funnel.items()
        ))
        self.stdout.write(
            "marketplace\tsource\tuse_mode\tstage\treason_code\ttotal\tlast_update"
        )
        rows = (
            projections
            .values("cupom__marketplace", "cupom__fonte__slug", "use_mode",
                    "stage", "reason_code")
            .annotate(total=Count("id"), last_update=Max("updated_at"))
            .order_by("cupom__marketplace", "cupom__fonte__slug", "use_mode",
                      "stage", "reason_code")
        )
        cutoff = timezone.now() - timezone.timedelta(
            minutes=max(1, options["stale_minutes"]),
        )
        stale = 0
        for row in rows:
            is_stale = bool(row["last_update"] and row["last_update"] < cutoff)
            stale += row["total"] if is_stale else 0
            self.stdout.write(
                "{cupom__marketplace}\t{cupom__fonte__slug}\t{use_mode}\t"
                "{stage}\t{reason_code}\t{total}\t{last_update}{suffix}".format(
                    **row, suffix="\tSTALE" if is_stale else "",
                )
            )
        if stale:
            self.stderr.write(self.style.WARNING(
                f"{stale} projeção(ões) sem atualização dentro do SLA.",
            ))
        alerts = diagnosticar_alertas_pipeline_cupons(agora=now)
        self.stdout.write("ALERTS\t" + "\t".join(
            f"{reason}={total}" for reason, total in alerts.items()
        ))
        latest_by_source = {}
        for run in ExecucaoIngestao.objects.select_related("fonte").order_by(
                "fonte_id", "-iniciada_em"):
            latest_by_source.setdefault(run.fonte_id, run)
        for run in latest_by_source.values():
            rejections = run.rejeicoes if isinstance(run.rejeicoes, dict) else {}
            self.stdout.write(
                f"SOURCE\t{run.fonte.marketplace}\t{run.fonte.slug}\t"
                f"status={run.status}\thealth={run.health_status}\t"
                f"seen={(run.metricas or {}).get('items_seen', 0)}\t"
                f"accepted={(run.metricas or {}).get('accepted', run.total_cupons)}\t"
                f"rejected={sum(int(value or 0) for value in rejections.values())}\t"
                + ",".join(f"{reason}:{total}" for reason, total in sorted(rejections.items()))
            )
