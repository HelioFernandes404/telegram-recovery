# Auditoria e acompanhamento de lotes

Use somente o lote e o canal autorizados. Em consulta de status, abra os bancos
somente para leitura, por exemplo `sqlite3.connect(path.resolve().as_uri() +
"?mode=ro", uri=True)` ou `Database(path, readonly=True)`. O construtor padrão de
`Database` pode escrever/migrar o banco e não é necessário para monitoramento.

## Fontes de evidência

| Fonte | O que conferir |
| --- | --- |
| `plan.json` | Canal, conjunto exato de IDs, documentos, tamanhos, destinos, exclusões |
| `status.json` | Estado, `run_id`, atividade, bytes, verificados, pendentes e IDs ativos |
| `audit.sqlite3` | `audits.message_id`, `run_id`, `ok`, `sha256`, `duplicate_of`, `details` |
| `data/manifest.sqlite3` | Registros de downloads selecionados, tentativas e códigos de erro |
| JSONL apontado por `status.log` | Eventos de progresso, retries, falhas e encerramento |
| Processo/serviço | Ativo ou encerrado, resultado e existência de outro executor |

Valide que os caminhos consultados pertencem ao projeto. Evite despejar logs ou
legendas inteiras: resuma IDs, contagens, horários e códigos. Compare contadores
entre checagens; `updated_at` avança periodicamente mesmo sem transferência.
Um serviço `active` sozinho não comprova progresso; um processo encerrado com
código 0 sozinho não comprova a auditoria de todos os arquivos.

As auditorias são atualizadas por mensagem, podendo coexistir linhas de execuções
anteriores. Consulte sempre o `run_id` atual e confronte os IDs com o plano, não
apenas a contagem total da tabela. `details` é JSON com `ok`, `error_code`, `sha256`,
`ffprobe` e `signature`; o hash deve corresponder ao registro do download.

```sql
SELECT ok, COUNT(*) FROM audits WHERE run_id = :run_id GROUP BY ok;
SELECT message_id, duplicate_of, checked_at FROM audits
WHERE run_id = :run_id AND duplicate_of IS NOT NULL;
SELECT message_id, details FROM audits WHERE run_id = :run_id;
```

Passe `run_id` como parâmetro SQL, sem interpolação. `ffprobe_available=true` no
status não basta: verifique `details.ffprobe='passed'` por arquivo. `unavailable`
precisa constar como limitação. SHA igual sinaliza cópia de conteúdo; preserve as
cópias e reporte apenas duplicados novos durante o acompanhamento.

## Segundo plano e monitoramento

Use execução persistente quando o usuário pedir que o lote continue depois da
conversa. No Linux deste projeto, um serviço transitório via `systemd-run --user`
é uma opção: configure WorkingDirectory na raiz, executável absoluto da `.venv`,
UMask=0077, Restart=no e TimeoutStopSec=90s, com saída em log privado do lote.
Não escreva configuração global de desktop ou habilite reinício ilimitado.
Verifique primeiro que não existe serviço/processo operando a mesma sessão.

Depois do início, confirme o serviço e avanço real no status/manifesto antes de
dizer que está baixando. A operação pode exigir permissão de rede/barramento fora
do sandbox; use a aprovação da ferramenta, sem tentar contornar a restrição.

Se houver mecanismo de monitoramento no produto, configure-o somente no escopo
solicitado. No Codex, use heartbeat da conversa e prefira atualizar um existente
a duplicar monitores. Um intervalo de 15 minutos foi usado neste projeto; ele é
uma convenção ajustável, não requisito do downloader. Salve canal, job, unidade,
critério de conclusão e limite de retomadas reais; não copie números de outro lote.
Se não houver agendador, informe a limitação sem prometer acompanhamento futuro.

Mantenha checagens saudáveis silenciosas, salvo pedido de atualizações periódicas.
Avise falha persistente, estagnação confirmada, duplicado novo, intervenção necessária
ou conclusão. Distinga suspensão/desligamento da máquina de progresso normal.

Se o usuário pedir para parar só o acompanhamento, pare o monitor e informe que o
download/auditoria por arquivo continuam. Se pedir para parar o backup, interrompa
o serviço exato com SIGTERM e confirme checkpoints/estado, sem matar processos
genéricos de Python. Pedidos ambíguos que possam interromper transferências exigem
explicitar a interpretação antes de afetar o serviço.

## Critérios de conclusão

Para declarar o lote concluído:

1. Confirme `state=completed`, ausência de IDs ativos e `pending=0`, mais resultado
   bem-sucedido do processo quando observável.
2. Confronte `plan.items` com auditorias aprovadas da execução atual, sem IDs extras
   ou ausentes, e com os downloads do canal/documento/tamanho/destino corretos.
3. Confirme existência e tamanho dos arquivos finais e ausência de seus `.part`,
   inclusive links simbólicos quebrados (`lstat`/`lexists`, não só `exists`).
4. Confira hashes registrados, resultados por arquivo e assinaturas atuais
   `[dev, inode, size, mtime_ns, ctime_ns]` contra as auditadas. Se mudou, não confie
   na auditoria antiga: use a conferência local de `audit_file`/`sha256_file` e
   investigue sem sobrescrever o arquivo. Não chame `run` só para auditar se puder
   baixar mídia ausente fora do escopo de uma consulta.
5. Reúna duplicados por SHA-256 e registre que permaneceram preservados.

O índice é uma referência de completude, não checksum. Se um grupo é numerado de
0 a 12, use 13 grupos no relatório. Não presuma que igualdade de contagens prova
igualdade dos conjuntos de tags/IDs.

## Preservação e relatório

Uma linha de base privada pode registrar downloads fora da seleção, os registros
do inventário, checkpoints e assinaturas dos arquivos existentes. Use hashes de
JSON canônico e mantenha as mesmas regras de ordenação/comparação no fechamento.
Não leia/copiei sessões nem credenciais para compor esse registro.

Lotes anteriores usaram `preservation-baseline.json`, mas ele é um artefato de
operação, não parte garantida do executor. Há duas grafias legadas para o digest:
`protected_downloads_sha256` e `protected_download_records_sha256`; confira qual
existe. Se a linha de base faltar, registre a limitação: não a reconstrua depois
para afirmar preservação retroativa. Assinaturas de arquivo são uma evidência de
preservação de metadados, não uma nova leitura integral de hash.

Grave um relatório local privado (0600) com canal/job/run_id, IDs e contagens
conferidos, bytes, duplicados, critérios aprovados/reprovados, preservação e
limitações. `ffprobe` não equivale a decodificação completa do vídeo. Não chame
`SHA-256 válido` de prova de autenticidade da origem: foi calculado localmente.
Ao finalizar ou depender de intervenção humana, encerre/ajuste o monitor pelo
mecanismo disponível e descreva a ação real: removido e pausado são estados diferentes.
