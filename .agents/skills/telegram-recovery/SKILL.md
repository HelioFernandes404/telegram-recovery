---
name: telegram-recovery
description: "Operar a CLI deste projeto para inventariar, baixar, retomar e auditar vídeos de canais autorizados do Telegram. Use para recuperar um canal, conferir um índice, verificar um lote ou investigar falhas do backup local. Não se aplica a bots ou automação do Telegram Web."
---

# Telegram Recovery

Use a implementação existente em `telegram_recovery/`. Localize a raiz pelo
`pyproject.toml` e pelo pacote; execute os comandos nessa raiz, não na pasta da skill.
Não recrie um downloader nem trate números de exemplos como alvos autorizados.

## Escopo e privacidade

- Respeite o canal, as exclusões e a etapa solicitada. Inventário, diagnóstico ou
  consulta de status não autorizam download. Uma recuperação completa autorizada
  permite inventariar e baixar o escopo indicado, sem reconfirmações repetidas.
- Use conta de usuário e API oficial/MTProto via Telethon. O acesso ao conteúdo é
  somente leitura: sem enviar, encaminhar, editar, apagar mensagens ou fazer upload.
  Não use Bot API nem Telegram Web para transferências.
- Reutilize a sessão local autorizada. `auth` exige pedido explícito e entrada do
  usuário no terminal local. Nunca peça telefone, código, senha 2FA, API hash ou
  arquivo de sessão na conversa. Não capture entradas do login por ferramentas.
- Não imprima `.env`, ambiente completo, conteúdo de `.session`, objetos do cliente
  ou exceções brutas do provedor. Use códigos sanitizados de `run_logging.py`.
  `.env`, sessões, SQLite, legendas, vídeos e logs são dados privados locais.
- Preserve backups concluídos, arquivos desconhecidos e alterações existentes.
  Não remova travas ou parciais para forçar execução e não substitua destinos
  conflitantes. O plano de outro canal não é um modelo de autorização.

## Escolha do procedimento

Leia somente a referência necessária ao trabalho atual:

- Novo canal, índice, filtros, download ou retomada: [operação](references/operacao.md).
- Lote em segundo plano, monitoramento, falha ou encerramento:
  [auditoria e acompanhamento](references/auditoria.md).

O [README do projeto](../../../README.md) detalha a instalação. Confira `--help`
e o código quando a documentação divergir; não execute operações reais apenas
para demonstrar ou validar esta skill.

## Particularidades que mudam as decisões

- `inventory` não transfere mídia. `download --dry-run`, `status` e `report` são
  consultas locais; uma prévia de download pode recalcular hashes dos arquivos.
- `download` confere tamanho e SHA-256. O executor `telegram_recovery.audited_batch`
  acrescenta ffprobe e registro de duplicados. O comando geral `verify` ainda
  retorna código 2: não o apresente como uma verificação funcional.
- A sessão e o manifesto têm travas compartilhadas. Antes de inventário/download,
  confira se já existe operação usando essa raiz. A presença do arquivo `.lock`
  não prova que a trava esteja ocupada; não o apague.
- Tags são independentes de IDs de mensagem. `F01`, `F001` e `F0001` normalizam para
  `F0001`. Nunca calcule ID de mensagem por deslocamento de tag.
- Conte os grupos do plano: módulo `0` é válido. Neste parser, `0.2 - Comece aqui`
  recebe a pasta `000_Comece_aqui`; os grupos 0–12 somam **13**, não 12.
- Títulos ou tags repetidos não bastam para deduplicar. Mesmo SHA-256 identifica
  conteúdo repetido; registre e preserve os arquivos. Isso não autoriza exclusão.
- Um SHA-256 calculado localmente detecta alteração posterior e igualdade entre
  arquivos; não é um checksum independente fornecido pelo Telegram. ffprobe
  verifica abertura e streams, sem decodificar integralmente todos os frames.

## Quando houver ajuste de código

Inspecione primeiro `naming.py`, `inventory.py`, `download_plan.py` ou o componente
responsável. Antes de mudar uma regra de legenda, teste com exemplos sintéticos
do novo formato e com os formatos antigos; trate ambiguidade sem inventar módulos.
Não altere nomes/identidades de um plano ativo, pois a retomada verifica o plano.
Execute os testes offline pertinentes e Ruff. `tests/conftest.py` bloqueia rede;
não use sessão real nos testes. Se for necessário atualizar metadados já vistos,
aplique `inventory --rescan` somente ao canal autorizado, após validar a correção
e quando não houver outro escritor. Não reescaneie canais concluídos sem pedido.

## Entrega

Informe canal e escopo, contagens reais, bytes, estado e evidências locais. Distinga
iniciado de concluído e resultados de auditoria de simples registros no manifesto.
Explique falhas e limitações materiais. Só prometa acompanhamento futuro depois
de confirmar que o mecanismo de monitoramento foi criado ou atualizado.
