import io
import unittest
from unittest.mock import MagicMock, patch

from apps.scrapers import playwright_runtime as runtime


class PlaywrightRuntimeTests(unittest.TestCase):
    def test_memoria_cgroup_le_uso_e_limite_em_megabytes(self):
        arquivos = [io.StringIO("1992294400\n"), io.StringIO("2147483648\n")]
        with patch("builtins.open", side_effect=arquivos):
            self.assertEqual(runtime._memoria_cgroup_mb(), (1900, 2048))

    def test_memoria_cgroup_v1_usa_limite_finito(self):
        def abrir(caminho, *args, **kwargs):
            valores = {
                "/sys/fs/cgroup/memory/memory.usage_in_bytes": "1048576000",
                "/sys/fs/cgroup/memory/memory.limit_in_bytes": "2147483648",
            }
            if caminho not in valores:
                raise FileNotFoundError(caminho)
            return io.StringIO(valores[caminho])

        with patch("builtins.open", side_effect=abrir):
            self.assertEqual(runtime._memoria_cgroup_mb(), (1000, 2048))

    def test_memoria_cgroup_v1_ilimitado_cai_para_meminfo(self):
        def abrir(caminho, *args, **kwargs):
            valores = {
                "/sys/fs/cgroup/memory/memory.usage_in_bytes": "1181368320",
                "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712",
            }
            if caminho not in valores:
                raise FileNotFoundError(caminho)
            return io.StringIO(valores[caminho])

        with patch("builtins.open", side_effect=abrir):
            self.assertIsNone(runtime._memoria_cgroup_mb())
        with patch.object(runtime, "_memoria_cgroup_mb", return_value=None), \
                patch.object(runtime, "_memoria_disponivel_mb", return_value=1200):
            self.assertIn("memoria_disponivel=1200MB", runtime.diagnostico_do_host())

    def test_diagnostico_usa_limite_da_vm_em_vez_da_memoria_do_host(self):
        with patch.object(runtime, "_memoria_cgroup_mb", return_value=(1900, 2048)), \
                patch.object(runtime, "_memoria_disponivel_mb", return_value=32000):
            diagnostico = runtime.diagnostico_do_host()
        self.assertIn("memoria_vm=1900/2048MB", diagnostico)
        self.assertIn("folga_vm=148MB", diagnostico)
        self.assertNotIn("32000", diagnostico)

    def test_driver_sem_playwright_recebe_erro_acionavel_com_causa(self):
        class Manager:
            def __enter__(self):
                return self._playwright

        with patch.object(runtime, "diagnostico_do_host", return_value="folga_vm=20MB"):
            with self.assertRaises(runtime.NavegadorIndisponivel) as capturado:
                with runtime.playwright_sincrono(Manager):
                    pass
        self.assertIn("folga_vm=20MB", str(capturado.exception))
        self.assertIsInstance(capturado.exception.__cause__, AttributeError)

    def test_attribute_error_nao_relacionado_nao_e_reclassificado(self):
        class Manager:
            def __enter__(self):
                return self.outro_atributo

        with self.assertRaises(AttributeError) as capturado:
            with runtime.playwright_sincrono(Manager):
                pass
        self.assertEqual(capturado.exception.name, "outro_atributo")

    def test_falha_no_desligamento_nao_apaga_excecao_do_trabalho(self):
        manager = MagicMock()
        manager.__enter__.return_value = object()
        manager.__exit__.side_effect = RuntimeError("driver caiu")
        with self.assertRaisesRegex(ValueError, "falha original"):
            with runtime.playwright_sincrono(lambda: manager):
                raise ValueError("falha original")
        manager.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
