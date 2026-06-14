"""
Canais de broadcast plugáveis (WhatsApp, Telegram, ...).

Cada canal implementa `Sender` (senders/base.py) e é registrado em senders/registry.py.
A orquestração (ofertas.py) resolve o canal por `ConfiguracaoEnvio.canal` via
`SENDERS.get(...)` — adicionar um canal novo = um arquivo + uma linha no registry.
"""
