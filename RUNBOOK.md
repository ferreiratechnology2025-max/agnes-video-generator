# ViMax Brain — Manual de Execução

**Versão:** 5.0.0  
**Data:** 2026-09-29  
**Status:** 🟢 Produção (Arquitetura Congelada)

---

## Sumário

1. [Requisitos](#1-requisitos)
2. [Instalação](#2-instalação)
3. [Configuração](#3-configuração)
4. [Execução do Daemon](#4-execução-do-daemon)
5. [Webhook Receiver (Teste)](#5-webhook-receiver-teste)
6. [Testes de Validação](#6-testes-de-validação)
7. [Monitoramento](#7-monitoramento)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Requisitos

### Hardware
- CPU: 2+ cores
- RAM: 4GB mínimo
- Disco: 1GB livre

### Software
- Python 3.10+
- SQLite 3 (incluído na stdlib)
- FFmpeg (para composição de vídeo)

### Dependências Python
```bash
pip install -r requirements.txt
```

---

## 2. Instalação

### Opção A: Instalação Rápida (Recomendada)
```bash
./start.sh
```

### Opção B: Instalação Manual
```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Verificação
```bash
python -c "from core.worker.daemon import WorkerDaemon; print('OK')"
```

---

## 3. Configuração

### Variáveis de Ambiente

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `AGNES_API_KEY` | — | API key da Agnes AI |
| `AGNES_WORKER_503_SUSPEND_MIN` | 30 | Minutos de suspensão após 503 |
| `AGNES_WORKER_POLL_INTERVAL` | 5 | Intervalo de polling (segundos) |
| `AGNES_WORKER_MAX_CONCURRENT` | 3 | Máximo de tarefas concorrentes |
| `AGNES_WORKER_WEBHOOK_TIMEOUT` | 10 | Timeout do webhook (segundos) |
| `AGNES_LOG_FILE` | "" | Arquivo de log (opcional) |
| `AGNES_R2_ENDPOINT_URL` | — | R2 endpoint (para I2V tunnel) |
| `AGNES_R2_ACCESS_KEY` | — | R2 access key |
| `AGNES_R2_SECRET_KEY` | — | R2 secret key |
| `AGNES_R2_PUBLIC_DOMAIN` | — | Dominio público R2 |

### Arquivo .env (Opcional)
```env
AGNES_API_KEY=sk-your-key-here
AGNES_WORKER_503_SUSPEND_MIN=30
AGNES_WORKER_POLL_INTERVAL=5
AGNES_WORKER_MAX_CONCURRENT=3
```

---

## 4. Execução do Daemon

### Modo Padrão
```bash
python -m core.worker.daemon
```

### Com Log em Arquivo
```bash
AGNES_LOG_FILE=daemon.log python -m core.worker.daemon
```

### Sinais de Controle

| Sinal | Comportamento |
|-------|---------------|
| `SIGINT` (Ctrl+C) | Shutdown gracioso — aguarda tarefas ativas |
| `SIGTERM` | Shutdown gracioso — aguarda tarefas ativas |
| `SIGINT` x2 | Força exit (código 1) |

### Log de Telemetria (JSON)
```json
{
  "timestamp": "2026-09-29T20:13:16.907931Z",
  "level": "INFO",
  "logger": "core.worker.daemon",
  "message": "[Telemetry] task_id=phase5-test-001 status=completed duration=0.5s retries=0 r2_frames=0"
}
```

---

## 5. Webhook Receiver (Teste)

### Iniciar Recebedor Mock
```bash
python scripts/mock_webhook_receiver.py
```

### URL de Callback
```
http://localhost:8080/webhook
```

### Payload Recebido
```json
{
  "event": "video.completed",
  "task_id": "phase5-test-001",
  "video_path": "/tmp/phase5-test-001.mp4",
  "timestamp": "2026-09-29T20:13:16.657865Z"
}
```

### Resposta
- HTTP 200 OK para todas as requisições
- Content-Type: application/json

---

## 6. Testes de Validação

### Teste Rápido (Unidades)
```bash
python scripts/phase5_validation.py --test all
```

### Teste de Integração (End-to-End)
```bash
python scripts/phase5_integration_test.py
```

### Teste por Componente
```bash
python scripts/phase5_validation.py --test shutdown
python scripts/phase5_validation.py --test telemetry
python scripts/phase5_validation.py --test webhook
```

---

## 7. Monitoramento

### Logs Estruturados (JSON)
Todos os logs seguem formato JSON para fácil parsing:
```bash
python -m core.worker.daemon 2>&1 | jq '.'
```

### Métricas de Telemetria
- `duration` — tempo total da tarefa (segundos)
- `retries` — quantidade de retries
- `r2_frames` — frames processados via R2

### Verificação de Status
```bash
# Verificar tarefas pendentes
sqlite3 .working_dir/state.db "SELECT task_id, status FROM tasks WHERE status='pending';"

# Verificar tarefas completadas
sqlite3 .working_dir/state.db "SELECT task_id, updated_at FROM tasks WHERE status='completed' ORDER BY updated_at DESC LIMIT 10;"
```

---

## 8. Troubleshooting

### Problema: Task não inicia
```
[WorkerDaemon] No pipeline for task {task_id}
```
**Solução:** Verificar se o pipeline_factory está configurado corretamente.

### Problema: 503 persistente
```
[StateManager] {task_id} SUSPENDED for 503, retry at {timestamp}
```
**Solução:** Aguardar回退时间（默认30分钟）后自动重试。

### Problema: Webhook falha
```
[WorkerDaemon] Webhook POST failed: Connection refused
```
**Solução:** Verificar se o receiver está rodando na porta 8080.

### Problema: SQLite lock
```
database is locked
```
**Solução:** Verificar se não há múltiplos processos acessando o mesmo DB.

---

## Arquitetura Congelada

```
ViMax Brain + I2V Tunnel + State Machine (SQLite)
├── core/db/state_manager.py      # State machine persistente
├── core/worker/daemon.py         # Daemon com graceful shutdown
└── scripts/
    ├── mock_webhook_receiver.py  # Validação webhook
    └── phase5_integration_test.py # Teste end-to-end
```

**Não modificar:**
- ❌ Migrar SQLite para outro SGBD
- ❌ Adicionar Celery/filas externas  
- ❌ Escalonar horizontalmente workers

---

## Suporte

- Documentação: `docs/public/phase5_freeze.md`
- Código: `core/worker/daemon.py`
- Testes: `scripts/phase5_*.py`

---

*Manual gerado automaticamente — Fase 5: Deploy, Observabilidade e Congelamento*
