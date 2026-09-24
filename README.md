# telegram-recovery

CLI local para inventariar vídeos de canais autorizados via MTProto/Telethon, usando uma conta de usuário. O canal é informado em cada execução; nenhum canal da empresa fica embutido como autorização padrão.

Esta versão inclui **inventário, autenticação local e download por curso/módulo com retomada**. Os testes usam mensagens, logins e transferências simulados, com conexões de rede bloqueadas. A verificação independente com ffprobe permanece para a próxima etapa.

| Comando | Disponibilidade nesta versão |
| --- | --- |
| `inventory` | Funcional, exige uma sessão de usuário previamente autenticada |
| `status` | Funcional, resumo do SQLite, sem rede |
| `report` | Funcional, registros em JSON Lines, sem rede |
| `auth` | Funcional, login explícito de usuário no terminal ou em wizard web local, com código e 2FA |
| `download` | Funcional; seleção local por curso/módulo/tag/mensagem, `.part`, SHA-256 e `--dry-run` |
| `verify` | Reservado; retorna código 2, sem executar ffprobe |

## Skill do time

A skill [telegram-recovery](.agents/skills/telegram-recovery/SKILL.md) fica neste
repositório, em `.agents/skills/telegram-recovery/`. Ela orienta inventário,
seleção, download, retomada e auditoria usando a implementação existente.
Para usá-la no Codex com este projeto aberto:

```text
Use $telegram-recovery para recuperar os vídeos do canal autorizado que vou informar.
Use $telegram-recovery para conferir o status e a auditoria do lote informado, sem iniciar downloads.
```

Versione a pasta da skill junto com o código para compartilhá-la com o time.
Cada integrante usa suas próprias credenciais e sessão locais; esses arquivos
permanecem ignorados pelo Git. A skill não concede autorização para outros canais
nem inicia login automaticamente. Se ainda não aparecer na lista da conversa
atual, abra uma nova tarefa neste mesmo projeto ou indique o caminho do `SKILL.md`.

## Instalação

Requer Python 3.11+ e Linux/macOS (permissões POSIX e `flock`). O ambiente deste projeto foi preparado com `uv` e Python 3.11. `ffprobe` é opcional e será usado apenas na etapa de verificação.

Na pasta do projeto:

```bash
uv sync --locked --python 3.11
uv run --no-sync telegram-recovery --help
uv run --no-sync pytest
```

O `uv.lock` fixa as versões usadas. Alternativa com `venv`, sem instalar pacotes no Python do sistema:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
.venv/bin/python -m pytest
```

A instalação por pip usa os intervalos do `pyproject.toml`; use `uv sync --locked` para reproduzir exatamente o ambiente de desenvolvimento.

## Publicação no GitHub

O checkout inclui um workflow de CI para Python 3.11, testes e Ruff. Antes do
primeiro commit, confirme que somente código, testes, documentação, skill e
arquivos de configuração de exemplo estão no staging:

```bash
git status --short --ignored
git add .
git diff --cached --check
git diff --cached --stat
git status --short
```

`.env`, sessões, SQLite, downloads, logs e parciais ficam fora do commit pelo
`.gitignore`. Revise o resultado de `git status --short` antes de publicar. O
projeto não declara uma licença por padrão; escolha a licença da empresa antes
de tornar o repositório público.

Depois de criar o repositório vazio no GitHub, associe o remoto e publique a
branch escolhida pela equipe:

```bash
git remote add origin https://github.com/ORGANIZACAO/telegram-recovery.git
git branch -M main
git push -u origin main
```

Use um repositório privado enquanto o time não tiver decidido a política de
licença e exposição dos detalhes operacionais.

## Configuração local

As opções `--root` ficam **depois do subcomando**. Por padrão, a raiz é o diretório atual. Use sempre a mesma pasta para reencontrar a sessão e o manifesto.

```text
telegram_recovery/       implementação
tests/                   testes offline
data/manifest.sqlite3    manifesto e pontos de retomada
sessions/telegram-recovery.session
downloads/               canal/curso/módulo/arquivos de vídeo
logs/inventory-*.jsonl    eventos e discovery de cada execução (privados)
logs/auth-*.jsonl         etapas de autenticação, sem valores digitados
logs/download-*.jsonl     progresso, tentativas e resultados dos downloads
.env                     configuração privada opcional
```

`TELEGRAM_API_ID` e `TELEGRAM_API_HASH` podem vir do ambiente ou de `.env` na raiz. O ambiente tem precedência, inclusive se uma variável estiver vazia. O programa não procura `.env` em pastas superiores nem interpola outros valores do ambiente. O arquivo `.env.example` contém somente campos vazios; para preparar um arquivo local sem sobrescrever um existente:

```bash
test -e .env || (umask 077; cp .env.example .env)
chmod 600 .env
```

Preencha os campos apenas em um editor local, com os dados da sua aplicação obtidos pelo [procedimento oficial do Telegram](https://core.telegram.org/api/obtaining_api_id). Nunca cole credenciais na conversa, em issues ou no Git. Não há parâmetros de CLI para telefone, código de login, senha 2FA ou API hash.

**O login acontece apenas no comando `auth`.** O inventário e o download usam `sessions/telegram-recovery.session` já autenticada. Sem esse arquivo, encerra antes de criar o cliente ou acessar a rede e não pede credenciais. Com uma sessão expirada, encerra após verificar a autorização, também sem abrir login. Sessões de bot são recusadas.

As pastas de sessão/dados ficam com permissão `0700`; arquivos de sessão/manifesto, `0600`. Há travas compartilhadas para impedir inventário, download e autenticação simultâneos sobre a mesma sessão ou manifesto. `.gitignore` exclui configuração privada, sessões, SQLite, downloads, arquivos `.part`, logs e ambiente virtual. As legendas também são conteúdo privado: o manifesto permanece local e `report` só escreve no terminal.

## Configuração e autenticação no terminal local

Na raiz do projeto, abra um terminal local e execute:

```bash
uv run --no-sync telegram-recovery auth --configure
```

O fluxo solicita, somente quando necessário:

1. `TELEGRAM_API_ID` e `TELEGRAM_API_HASH`, obtidos em **API development tools** no [portal oficial do Telegram](https://my.telegram.org). Ambos são digitados com entrada oculta e salvos no `.env` local com permissão `0600`.
2. Telefone no formato internacional, com `+` e DDI. Por preferência explícita do usuário, ele fica **visível somente no terminal local** para conferir a digitação; não entra nos logs.
3. Código de login recebido pelo canal escolhido pelo Telegram, digitado somente nesse terminal.
4. Senha 2FA, se exigida. A senha mantém espaços e outros caracteres exatamente como digitados.

O telefone fica visível localmente; API ID, API hash, código e senha 2FA permanecem ocultos. O comando não aceita segredos por argumentos, pipes ou entrada/saída redirecionada. Se o terminal não permitir ocultar os campos secretos, encerra antes de tentar ler o valor. Não cole esses dados no chat ou no histórico de comandos.

`--configure` preenche apenas campos vazios/ausentes. Configuração válida é reutilizada sem perguntar os valores novamente; comentários e demais chaves são preservados. As atribuições novas são acrescentadas no final do arquivo, após eventuais placeholders vazios, e o carregador utiliza a última ocorrência. O arquivo é substituído atomicamente por um temporário privado, ignorado pelo Git. Se detectar uma edição externa durante os prompts, preserva a edição e encerra. Credenciais preenchidas mas inválidas não são sobrescritas: corrija-as no editor local. Overrides de ambiente incompletos/inválidos também devem ser corrigidos ou removidos antes de usar `--configure`.

Se as credenciais já estão configuradas, basta:

```bash
uv run --no-sync telegram-recovery auth
```

Uma sessão de usuário já autorizada é reutilizada sem pedir telefone/código. Não há logout, troca forçada de conta, exclusão de sessão ou cadastro de conta nova. A sessão é criada com permissão `0600` em uma pasta `0700`; o mesmo lock usado pelo inventário impede autenticação simultânea sobre ela. A confirmação de sucesso aparece após desconectar e finalizar a gravação da sessão.

Códigos e senhas incorretos permitem até três tentativas de envio ao Telegram; entradas de formato inválido têm até três tentativas locais por prompt. Código expirado, FloodWait, timeout ou falha de rede encerram sem reenvio automático de código pela aplicação. Uma resposta de rede incerta pode exigir apenas verificar a sessão na próxima execução de `auth`. `Ctrl+C` cancela o fluxo e preserva a configuração/sessão já gravadas.

Há uma exceção para o redirecionamento explícito de servidor (erro 303: `PHONE_MIGRATE`, `NETWORK_MIGRATE` ou `USER_MIGRATE`): o Telethon muda de servidor e a aplicação permite repetir a operação rejeitada uma vez. Isso segue a [orientação oficial de redirecionamento do Telegram](https://core.telegram.org/api/errors#303-see-other), sem repetir entregas de resultado incerto. O evento `auth_dc_redirect` registra essa etapa sem identificar a conta. Redirecionamentos repetidos encerram com `auth_dc_migration`. Erros do provedor são preservados por tipo antes da sanitização, evitando que a biblioteca transforme esses casos em um `ValueError` genérico. `value_error`, `runtime_error` e `type_error` registram categorias locais, nunca o texto bruto da exceção.

O log `auth-*.jsonl` registra somente etapas e códigos de diagnóstico: `auth_started`, `auth_prompt` (tipo do campo, nunca o valor), `auth_request`, `auth_retry`, `auth_step` e `run_finished`. Não registra identidade da conta, resposta bruta do Telegram, telefone, código, senha, hash ou arquivo de sessão. O telefone do modo terminal aparece apenas na tela local, nunca nos logs. O comando não lê o histórico do canal nem inicia downloads.

## Login guiado no navegador local

Para conduzir a autenticação por uma interface passo a passo, execute na raiz do projeto:

```bash
uv run --no-sync telegram-recovery auth --web
```

Abra a URL exibida no terminal, normalmente `http://127.0.0.1:8766`. O servidor aceita conexões somente de loopback, usa um cookie de sessão temporário e mantém o cliente Telethon no processo local. A interface orienta a configuração do API ID/API hash, telefone, código e senha 2FA; não grava segredos no navegador, na URL ou nos logs. O botão de cancelamento encerra a sessão e libera a trava local.

O login pelo terminal continua disponível com `auth` e `auth --configure`. Não execute o wizard web em uma porta exposta à rede nem compartilhe a URL durante uma autenticação.

Diagnósticos adicionais: `auth_terminal_required` (terminal sem suporte), `auth_input_closed` (entrada encerrada), `auth_config_changed` (edição concorrente), `auth_attempts_exhausted` (tentativas esgotadas), `auth_code_expired` (código expirado), `auth_flood_wait` (esperar o prazo informado), `auth_network` (conexão incerta), `auth_phone_invalid` (telefone recusado), `auth_account_missing` (conta não cadastrada) e `auth_flow_unsupported` (fluxo de login diferente do implementado).

## Primeiro inventário após a autenticação

Execute na raiz do projeto:

```bash
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --limit 100
```

Esse comando lê somente metadados de **até 100 mensagens** se já houver uma sessão autorizada e configuração válida. Sem sessão, apenas informa o pré-requisito e termina. Ele não autentica nem baixa vídeos.

## Inventário e filtros

```bash
# Continuar do ponto salvo; sem limite, percorre até não haver mais mensagens
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID"

# Uma ou várias tags; repetições de --tag funcionam como OU
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --tag F0001 --limit 100
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --tag '#F0001' --tag F0002

# Intervalo numérico inclusivo (não pressupõe ordem de tags no canal)
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --from-tag F0001 --to-tag F0010

# Rever mensagens antigas, inclusive alterações em legendas/mídias
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --rescan --limit 100

# Consultar somente uma mensagem pelo ID, preservando o checkpoint do histórico
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --message-id 42

# Fixar a raiz quando estiver em outro diretório
telegram-recovery inventory --root /caminho/telegram-recovery --channel "$CHANNEL_ID" --limit 100
```

- A leitura acontece do mais antigo para o mais recente, em páginas de até 100 mensagens, com pausa de 1 segundo entre páginas. `--page-size` permite valores de 1 a 100.
- `--limit` limita **mensagens examinadas nesta execução**, incluindo mensagens de texto e vídeos excluídos pelos filtros. Não é uma quantidade de vídeos encontrados. Ao atingir esse limite, o resumo indica `limited`; somente uma página vazia confirma `idle` (sem mais mensagens naquele momento).
- Sem filtros de tag, todos os vídeos encontrados são preservados, inclusive sem `#F`. Com filtros, somente vídeos correspondentes são inseridos/atualizados; registros existentes nunca são apagados. Uma mensagem pode conter várias tags. `--tag` combinado com intervalo exige que uma mesma tag satisfaça ambos.
- Tags aceitam `F01`, `F001`, `F2072` ou `'#F2072'`, sem distinguir maiúsculas de minúsculas, com dois ou mais dígitos. Para preservar os registros e filtros existentes, a forma interna tem no mínimo quatro dígitos: `F01`, `F001` e `F0001` representam a mesma tag. A legenda original permanece intacta. Coloque aspas ao usar `#` no shell. Intervalos são numéricos e inclusivos.
- Cada canal e combinação de filtros têm um checkpoint independente. Alterar `--limit` ou `--page-size` mantém o mesmo checkpoint. Uma primeira busca filtrada não impede uma busca posterior sem filtro de percorrer o histórico inteiro.
- Cada página e o cursor são confirmados em **uma transação**. A chave `(channel_id, message_id)` impede duplicação. `Ctrl+C` preserva páginas já confirmadas; repita o mesmo comando **sem `--rescan`** para continuar. Uma página interrompida antes da confirmação será relida.
- Depois de terminar, uma nova execução lê mensagens posteriores ao cursor. Para atualizar legendas ou mídias de mensagens antigas, use `--rescan`; isso reinicia apenas o cursor daquele filtro, preservando os registros. Depois de uma varredura limitada/interrompida, continue sem `--rescan`.
- Conteúdo apagado do Telegram não é apagado do manifesto. Esta versão não reconcilia exclusões, edições que deixam de corresponder ao filtro ou vídeos transformados em outros tipos de mensagem.

`--message-id` faz uma consulta direta e atualiza somente os metadados daquela mensagem. O ID da mensagem é diferente da tag da aula; essa relação nunca deve ser calculada por soma. Repetir a consulta atualiza o mesmo registro, sem duplicá-lo. Esse modo não avança, reinicia nem cria checkpoints de histórico e não pode ser combinado com filtros de tag, `--limit`, `--rescan` ou tamanho de página diferente do padrão. Usa as mesmas proteções de sessão, timeout e novas tentativas do inventário paginado.

O resumo individual informa `video`, `missing` ou `non_video`. Uma resposta ausente ou sem vídeo preserva os registros anteriores, sem afirmar que o arquivo ainda está disponível. Uma resposta de outro canal/ID encerra com `message_mismatch` sem gravar metadados. Nenhuma dessas consultas transfere o vídeo ou valida seus bytes.

Os campos armazenados são `channel_id`, `message_id`, `date` (ISO 8601 UTC), `caption`, `tags` (JSON), `title`, `original_filename`, `mime_type`, `expected_size` (bytes), `duration` (segundos), `document_id`, `suggested_filename`, `status`, `first_seen_at` e `updated_at`. Metadados ausentes ficam como `NULL`; mensagens sem tags recebem `[]`. Não são persistidos `access_hash`, referências de arquivo, objetos da conta ou credenciais no manifesto.

São reconhecidos documentos por atributo de vídeo, MIME `video/*` ou extensão de vídeo conhecida. A extensão é uma heurística para documentos enviados como `application/octet-stream`; a confirmação do conteúdo será responsabilidade da verificação futura. O texto original é preservado integralmente no SQLite.

Quando a legenda contém uma sequência de curso, módulo e aula, a extração reconhece essa hierarquia e inclui os números no nome sugerido. Isso evita chamar todas as aulas de “aula”, mesmo quando todos os arquivos originais se chamam `aula.mp4`. Hierarquias incompletas, fora de ordem ou ambíguas usam o comportamento geral: primeira linha não vazia após retirar tags `#F`, com fallback para o nome do arquivo ou ID da mensagem. Para corrigir registros antigos, consulte cada `--message-id` ou use `--rescan`; atualizar o código sozinho não reprocessa o manifesto.

O status inicial é `inventoried`: significa apenas que os metadados foram coletados. Uma alteração no documento ou tamanho detectada durante nova leitura gera `media_changed`. A sugestão de nome elimina componentes de caminho e caracteres inseguros, limita seu comprimento e inclui o ID da mensagem, por exemplo `F0001__001_Curso__001__001_Aula__m42.mp4`; não cria nem renomeia arquivos. O downloader usa pastas por canal, curso e módulo e recusa conflitos com arquivos existentes.

O progresso vai para stderr; o resumo final vai para stdout, com mensagens lidas, vídeos encontrados, selecionados, novos/atualizados, sem tag e cursor. Nenhum conteúdo de legenda, nome da conta ou erro bruto do provedor é impresso durante o inventário. `FloodWait` de até 300 segundos é respeitado, com no máximo quatro tentativas por leitura; esperas maiores interrompem com orientação de retomada. Timeout, rede e erros transitórios do servidor têm novas tentativas com recuo. Outros erros encerram preservando o último checkpoint.

## Logs para a sessão guiada e discovery

Cada execução válida de `inventory` cria um arquivo exclusivo em `logs/inventory-<UTC>-<run_id>.jsonl`, inclusive quando a sessão estiver ausente. O caminho relativo aparece no terminal. A pasta recebe permissão `0700` e cada arquivo, `0600`; arquivos anteriores são preservados, sem sobrescrita ou exclusão automática. Tudo continua local e ignorado pelo Git. Erros de sintaxe/filtros são recusados antes de iniciar uma execução e não criam log.

O modo normal mostra progresso com horário UTC e ID curto da execução. Para acompanhar também os eventos detalhados durante a sessão guiada:

```bash
uv run --no-sync telegram-recovery inventory --channel "$CHANNEL_ID" --limit 100 --verbose
```

O log estruturado é o mesmo com ou sem `--verbose`; a opção apenas aumenta o detalhe no terminal. Ela **não habilita logs brutos do Telethon**. O inventário continua sem transferir arquivos, solicitar credenciais ou abrir login. Transferências exigem o comando explícito `download`.

Cada linha é um objeto JSON com `schema_version`, `timestamp`, `run_id`, `sequence`, `event`, `level` (`INFO`, `WARNING`, `ERROR`) e `elapsed_seconds`. Os principais eventos são:

| Evento | Informação útil |
| --- | --- |
| `run_started` / `preflight` | Canal, filtros normalizados, limite, tamanho da página, modo de releitura, versões de Python/Telethon/aplicação, disponibilidade de ffprobe e existência de sessão/manifesto; sem ler ou registrar valores de credenciais |
| `read_started` / `read_finished` | Etapa (`connect`, `authorization`, `user`, `channel`, `dialogs`, `history`, `message`), tentativa e duração da leitura |
| `read_retry` / `read_failed` | Motivo padronizado, número da tentativa e espera solicitada; nenhum texto da exceção |
| `channel_cache_miss` / `channel_resolved` | Necessidade de consultar diálogos e origem da resolução; ausência no cache é informativa, não uma falha |
| `page_requested` / `page_committed` | Cursor anterior, quantidade solicitada, intervalo de IDs, mensagens lidas, vídeos selecionados, inseridos/atualizados, duração da página e contagens de metadados |
| `message_parse_failed` | ID da mensagem cujos metadados não puderam ser extraídos, sem sua legenda/nome de arquivo |
| `message_requested` / `message_inventory_finished` | Consulta individual: ID, resultado (`video`, `missing`, `non_video`), inseridos e atualizados; nenhum avanço de cursor |
| `inventory_finished` | Resultado (`idle`, `limited`, `failed`, `interrupted`), cursor inicial/final, velocidade média e resumo `discovery` |
| `run_finished` | Código de saída, motivo padronizado de falha, último cursor confirmado, número de operações de leitura e novas tentativas |

`read_calls` conta operações de alto nível, incluindo tentativas; não representa o número exato de requisições MTProto internas. `retry_wait_seconds` soma as esperas programadas; uma interrupção pode encurtar a espera efetiva. Durações usam relógio monotônico, enquanto os horários dos eventos usam UTC.

O campo `inventory_finished.discovery` resume **todos os vídeos observados nas páginas confirmadas desta execução**, incluindo os excluídos pelo filtro. Isso permite descobrir o perfil do canal sem inserir registros fora do filtro. Ele inclui:

- Mensagens com/sem vídeo, vídeos selecionados e excluídos, sem tag ou com várias tags.
- Nome original/MIME/duração ausentes, tamanho desconhecido, soma dos tamanhos e durações conhecidos. São valores esperados dos metadados, não bytes baixados ou conteúdo validado.
- `structured_captions`: quantidade de legendas com a hierarquia curso/módulo/aula reconhecida; não registra o texto desses títulos.
- Contagens de MIME em categorias conhecidas; valores não reconhecidos são agrupados, sem copiar texto arbitrário para o log.
- Quantidade de tags distintas, menor/maior número observado, tags presentes em mais de uma mensagem e intervalos numéricos não observados entre as tags vistas. As amostras de repetições e intervalos têm no máximo 20 entradas; não são geradas listas enormes para intervalos extensos.

Nos campos de tags, o número `2072` corresponde a `F2072`. **Tags repetidas não comprovam arquivos duplicados; lacunas numéricas não comprovam vídeos ausentes.** Elas podem decorrer de filtros, limites, retomada, convenções do canal ou itens que ainda não foram vistos. Vídeos sem tag não participam dessa análise de sequência. A detecção por conteúdo entre arquivos/módulos distintos permanece para a verificação futura; a checagem de módulos já baixados está descrita abaixo.

`history_exhausted=true` significa que não vieram mais mensagens após o cursor nesta leitura. `scanned_from_start=true` indica que a execução começou com cursor zero. Para avaliar uma varredura desde o início, confira ambos e o estado `idle`. Uma execução com `--limit` pode estar parcial mesmo se a última página parecer completa. Nenhum desses campos afirma que conteúdo apagado do Telegram foi recuperado.

Em consultas com `--message-id`, `run_started.message_id` identifica o alvo e `message_inventory_finished` substitui o resumo de varredura. Não há evento `inventory_finished`, estatísticas de páginas nem declaração de histórico esgotado. `last_committed_cursor=null` no log individual significa que essa execução não confirmou um cursor; o checkpoint anterior no SQLite permanece intacto.

Os diagnósticos usam códigos estáveis para orientar a sessão guiada:

| Código | Interpretação / próximo passo |
| --- | --- |
| `session_missing` | Sessão ainda não existe; nenhum acesso ao Telegram foi iniciado |
| `credentials_invalid` | Configuração local ausente/inválida; revisar no editor local |
| `session_unauthorized` / `unauthorized` | Sessão sem autorização ou expirada; não há login automático |
| `bot_session` | É necessária uma sessão de conta de usuário |
| `channel_inaccessible` / `forbidden` | Conferir se essa conta possui acesso ao canal informado |
| `flood_wait` / `flood_wait_limit` | Espera do Telegram; o segundo código encerra quando a espera/budget excede a política |
| `timeout` / `network` / `telegram_server` / `read_retries_exhausted` | Falha transitória ou tentativas esgotadas; verificar o último cursor e retomar depois |
| `already_running` | Outra execução está usando a sessão ou o manifesto |
| `metadata_invalid` / `pagination_invalid` | Revisar o ID da mensagem ou cursor indicado antes de ampliar a coleta |
| `sqlite` / `local_io` / `manifest_version` | Conferir armazenamento, permissões ou versão do manifesto; `errno`, quando presente, é somente o código numérico do sistema |
| `interrupted` | Interrupção solicitada; retomar com os mesmos filtros, sem `--rescan` |
| `unexpected` / `telegram_rpc` / `bad_request` / `recovery_error` | Consultar a última etapa registrada e revisar o cenário localmente; detalhes brutos são omitidos |

Há uma lista permitida de campos/tipos por evento: legendas, títulos, nomes originais, telefone, hash, códigos, senha 2FA, referências de arquivo, objetos da conta e conteúdo de sessão não são serializados, inclusive em `--verbose`. O log ainda contém metadados do canal (IDs, tags e contagens), por isso permanece privado. O manifesto e `report` continuam preservando os metadados completos conforme o escopo original.

Eventos são descarregados para o arquivo a cada linha; ao finalizar, há sincronização com o disco. O SQLite é a fonte do checkpoint. Um encerramento forçado ou falha de armazenamento pode deixar o log sem `run_finished`; isso não deve ser interpretado como sucesso. Se não for possível criar o log, o inventário encerra antes de abrir a sessão. Os testes exercitam falhas e interrupções somente com dados simulados.

## Consultas locais

```bash
uv run --no-sync telegram-recovery status
uv run --no-sync telegram-recovery report --channel "$CHANNEL_ID"
```

`status` mostra contagens, bytes esperados conhecidos, tamanhos desconhecidos, vídeos sem tag, estados e checkpoints. `report` produz um objeto JSON por linha, com tags como lista. Nenhum deles precisa de credenciais ou acessa o Telegram. Um checkpoint `running` deixado após encerramento forçado pode ser retomado normalmente.

Códigos de saída: `0` sucesso (inclusive inventário limitado ou manifesto ainda ausente em consultas), `1` falha de sessão/rede/armazenamento, `2` uso inválido ou comando reservado e `130` interrupção por `Ctrl+C`.

## Download organizado por curso e módulo

O download trabalha sobre o manifesto já coletado. Não amplia o inventário automaticamente. A estrutura usa o nome e número do curso, do módulo e da aula:

```text
downloads/
  channel_<channel-id>/
    001_Curso_exemplo/
      001_Modulo_exemplo/
        F0001__001_Curso_exemplo__001__001_Aula__m42.mp4
```

O número do curso e do módulo é comparado numericamente: `001` e `1` selecionam o mesmo módulo. `--module` exige `--course`, pois cursos diferentes podem ter um módulo 001. Sem estrutura de curso/módulo reconhecida, o vídeo permanece elegível para download sem esses filtros, nas pastas `SEM_CURSO/SEM_MODULO`, inclusive quando não tem tag.

Os nomes de pastas são limitados e sanitizados; o primeiro registro inventariado de cada curso/módulo define o nome canônico. O ID da mensagem diferencia nomes iguais dentro do canal. Após registrar um download, seu caminho permanece fixo mesmo se a legenda mudar. Não há movimentação ou renomeação automática de arquivos já existentes.

Também são reconhecidas legendas com dois níveis, como `2. Fundamentos` seguido de `=2. Primeiros passos`. Nesse formato, o primeiro nível é o módulo e o segundo é a aula. Como não há nível de curso na legenda, o agrupamento local usa `--course 0`, na pasta `000_Curso_do_canal`; os módulos e aulas mantêm seus números reais. Não se infere módulo a partir de um deslocamento numérico na tag. Hierarquias ambíguas ou misturadas não são adivinhadas.

```bash
# Prévia de um módulo desse formato de canal
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --course 0 --module 2 --dry-run

# Baixar todo o inventário desse canal, com a mesma proteção contra duplicação
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --concurrency 1
```

Resultados de execuções reais, sumários, manifestos e relatórios ficam nas pastas locais ignoradas pelo Git. Não copie esses artefatos para issues, pull requests ou commits.

```bash
# Prévia local do módulo: não conecta, não lê .env/sessão e não altera SQLite/arquivos
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --course 116 --module 001 --dry-run

# Baixar somente a aula F2072, pela mensagem conhecida
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --message-id 42

# Baixar ou retomar somente um módulo, um vídeo por vez
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --course 116 --module 001

# Prévia do curso inteiro, ou seleção por tag
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --course 116 --dry-run
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --tag F0001 --dry-run

# Limitar a seleção e configurar concorrência
uv run --no-sync telegram-recovery download --channel "$CHANNEL_ID" --course 116 --limit 2 --concurrency 1
```

Sem `--dry-run`, o comando transfere os vídeos selecionados usando a sessão existente. Sem filtros/limite, seleciona todo o canal presente no manifesto. `--concurrency` aceita de 1 a 8, com padrão 1. `--tag` é repetível; também há `--from-tag`, `--to-tag`, `--message-id`, `--root` e `--verbose`. Filtros de curso, módulo, tag e mensagem são combinados como interseção. A ordenação é curso, módulo, número da aula e ID da mensagem. `--limit` limita os vídeos selecionados, incluindo os já concluídos que serão conferidos/ignorados; não significa “próximos N ausentes”.

A prévia resume pastas, quantidade de vídeos, bytes esperados e até três caminhos de exemplo por módulo. `expected_bytes` é a soma de metadados selecionados, sem descontar arquivos existentes. A checagem local também informa `already_downloaded`, `pending_videos`, `modules_complete` e `module_checks` (aulas inventariadas, selecionadas e verificadas por módulo). `pending_expected_bytes` soma o tamanho integral dos vídeos pendentes selecionados; não desconta bytes de parciais e inclui conflitos que exigem revisão. Não afirma que um módulo esteja completo no Telegram: depende do inventário coletado. Resultados de pilotos e conferências devem permanecer em `logs/` e `data/` locais.

### Checagem automática de módulos já baixados

O check é automático, sem nova opção de linha de comando. Antes de abrir a sessão do Telegram, cada módulo selecionado é identificado por **canal + número do curso + número do módulo**. Nomes iguais ou módulo `001` em cursos diferentes não são tratados como duplicação.

Um módulo só é considerado completo quando **todas as suas aulas presentes no manifesto** têm documento/tamanho compatíveis, arquivo final existente e tamanho/SHA-256 conferidos com o registro do download. A checagem lê os arquivos locais para recalcular hashes, podendo demorar em módulos grandes. Não basta existir uma pasta ou um status antigo `downloaded`.

- Módulo completo: suas aulas selecionadas são ignoradas, com `modules_skipped` no resumo e `module_checked` no log. Se todas as aulas selecionadas já estiverem válidas, o comando termina sem conectar ao Telegram ou pedir login.
- Módulo incompleto: apenas aulas pendentes selecionadas seguem para o downloader. Arquivos conflitantes continuam preservados e geram erro, conforme as regras de retomada abaixo.
- Novas aulas no inventário, alteração de mídia/tamanho, arquivos ausentes ou SHA-256 divergente fazem o módulo deixar de ser completo na próxima checagem. Não há um marcador permanente que esconda novas aulas.
- `--limit`, `--tag` ou `--message-id` não transformam uma seleção parcial em módulo completo: a avaliação de completude considera também as outras aulas inventariadas daquele módulo. A seleção continua determinando quais aulas podem ser baixadas.

`download --dry-run` executa a mesma conferência sem escrever no manifesto, criar pastas ou abrir sessão. Vídeos sem hierarquia reconhecida recebem a checagem individual, sem declaração de módulo completo. Esse mecanismo evita repetir downloads registrados; não considera módulos com IDs diferentes equivalentes só por títulos ou conteúdo parecido.

### Retomada e preservação de arquivos

- Cada vídeo é gravado em um `.part` privado na mesma pasta do destino. Somente parciais criados e vinculados ao documento pelo manifesto podem ser retomados. O prefixo salvo é conferido por SHA-256 antes de acrescentar bytes.
- Antes de cada tentativa, a mensagem é consultada novamente para atualizar a referência. Se documento, tamanho ou canal/mensagem divergirem, o download encerra preservando o parcial. Referências expiradas, rede, timeout e falhas transitórias têm até quatro tentativas por execução; o timeout de transferência é de 45 segundos por bloco, não por vídeo inteiro.
- FloodWait de até 300 segundos é respeitado. Esperas maiores encerram aquele item. O lote registra falhas e continua com outros itens; cada item e tentativa têm eventos próprios.
- O progresso é sincronizado com o disco e SQLite aproximadamente a cada dois segundos e ao sair da transferência. O manifesto guarda bytes, tentativas acumuladas, SHA-256 do prefixo/final, motivo padronizado, estado e data de conclusão. Não guarda referências MTProto nem conteúdo de credenciais.
- Ao concluir, o tamanho deve ser exatamente o esperado. O SHA-256 é calculado lendo o parcial; então o arquivo é publicado atomicamente sem substituir um destino existente (`renameat2` com `RENAME_NOREPLACE` no Linux; fallback de hard link atômico seguido de remoção somente do nome temporário pertencente à transferência).
- Arquivos finais registrados só são ignorados depois de conferir tamanho e SHA-256. Arquivos/parciais desconhecidos, links simbólicos, arquivos alterados e destinos conflitantes ficam preservados e geram falha. Um arquivo final incompleto ou divergente não é truncado nem reparado automaticamente: requer revisão local do conflito. Apenas o `.part` reconhecido tem retomada automática.
- `Ctrl+C` preserva parciais e registra interrupção. Repita o mesmo comando para retomar. Uma falha após publicar o arquivo, mas antes de marcar a conclusão no SQLite, é recuperada pela conferência do tamanho/hash na próxima execução.

O recebimento de cada bloco usa `asyncio.timeout`, preservando o cancelamento da tarefa quando a chegada de um bloco coincide com a interrupção. Há testes de regressão para essa situação no Python 3.11, tanto no meio do arquivo quanto no último bloco; o parcial e seu checkpoint permanecem disponíveis para retomada.

O SQLite passa de schema 1 para 2 em uma transação ao ser aberto para escrita pela versão nova. A migração acrescenta `downloads` e preserva vídeos e checkpoints. `status`, `report` e `download --dry-run` abrem o manifesto somente para leitura e também aceitam schema 1 sem migrá-lo. Não use uma versão antiga da aplicação para escrever no manifesto após essa migração.

O `status` inclui contagens de downloads por estado: `pending`, `downloading`, `ready`, `downloaded`, `failed` e `interrupted`. Esses estados pertencem à tabela `downloads`; os estados do inventário continuam independentes. Downloads nunca avançam checkpoints de inventário.

Cada execução de `download` sem `--dry-run` cria `logs/download-<UTC>-<run_id>.jsonl`, mesmo quando todos os itens forem ignorados localmente. Os eventos incluem `download_started`, `module_checked`, `download_attempt`, `download_progress`, `download_retry`, `download_completed`, `download_skipped`, `download_failed` e `download_finished`. Os eventos têm somente IDs, contagens, indicadores de completude, bytes, tentativas, esperas e códigos de erro permitidos; nenhum título, caminho de arquivo, telefone, hash da API, código de login ou senha. `--verbose` preserva essa restrição. A prévia mostra caminhos por ser uma consulta local explícita, assim como `report` mostra metadados privados.

Uma falha em qualquer item faz o comando retornar 1 após resumir o lote, preservando os demais resultados. Diagnósticos incluem `file_conflict`, `path_invalid`, `checksum_mismatch`, `media_changed`, `message_missing`, `size_mismatch`, `file_reference_expired`, `flood_wait_limit` e `download_retries_exhausted`. Nenhuma mensagem ou mídia é alterada no Telegram.

## Verificação prevista

`verify` continua reservado (código 2). O download verifica tamanho e registra SHA-256; ainda não executa ffprobe, valida decodificação nem procura arquivos duplicados. Essas operações continuam para a etapa de verificação independente.

### Lotes com auditoria automática

O executor `telegram_recovery.audited_batch` acrescenta auditoria local ao downloader existente.
O comando `prepare` congela os IDs, documentos, tamanhos e destinos presentes no inventário,
sem conectar ao Telegram. Um plano existente nunca é substituído por outra preparação.
O comando `run` usa somente a sessão já autenticada, sem solicitar login.

```bash
# Seleção aprovada: complementos inteiros, exceto curso 001 já concluído e revisões.
.venv/bin/python -m telegram_recovery.audited_batch prepare \
  --job meu-lote-20260918 --channel "$CHANNEL_ID" \
  --exclude-course 1 --exclude-course 43 --exclude-course 47 \
  --exclude-course 78 --exclude-course 79 --exclude-course 84

# Executar ou retomar exatamente essa seleção; padrão de concorrência: 1.
.venv/bin/python -m telegram_recovery.audited_batch run \
  --job meu-lote-20260918 --concurrency 3

# Consulta local, inclusive enquanto o processo está rodando.
cat data/batches/meu-lote-20260918/status.json
```

O plano desse lote inclui os 15 cursos parcialmente repetidos por inteiro; a escolha dos trechos
fica para o estudo. O DevOps Pro e o curso 001 já concluídos ficam fora da seleção.

Antes de transferir, o executor confere os arquivos finais selecionados já existentes. Depois de
cada novo download, recalcula o SHA-256, confere tamanho e identidade no manifesto e executa
ffprobe se instalado. O ffprobe verifica abertura e presença de vídeo; **não decodifica todos os
frames**. Ausência de ffprobe é registrada explicitamente. Duplicados por SHA-256 dentro do lote
ficam registrados para revisão e nunca são apagados. Conflitos, falhas de auditoria ou esgotamento
das tentativas interrompem a fila e preservam arquivos e parciais para diagnóstico/retomada.

Cada lote guarda `plan.json`, `status.json` e `audit.sqlite3` privados em `data/batches/<job>/`.
O status é publicado atomicamente a cada dez segundos e após auditorias, com contagens,
bytes, IDs ativos, códigos de erro e caminho do log JSONL. `completed` exige todas as auditorias
aprovadas e uma conferência final de que nenhum arquivo auditado foi alterado ou deixou parcial.
As auditorias são refeitas na retomada; mudar o plano ou a identidade da mídia exige revisão.
Os estados de atenção são `needs_attention` e `interrupted`. O bloqueio compartilhado do
manifesto impede iniciar um segundo downloader/inventário ao mesmo tempo.

Para execução independente do terminal, pode-se usar um serviço transitório do usuário com
`systemd-run --user`, diretório de trabalho deste projeto e `UMask=0077`. O serviço não deve
reiniciar indefinidamente em caso de falha. Pare-o com `systemctl --user stop <unidade>`:
SIGTERM cancela as transferências e salva seus checkpoints. Consulte `status.json` e o resultado
do serviço antes de retomar. A máquina precisa permanecer ligada e conectada; suspensão ou
desligamento interrompem o trabalho. O comando geral `verify` continua reservado.

## Testes e escopo de acesso

```bash
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

Os testes usam tipos locais do Telethon e clientes simulados. Um fixture bloqueia resolução DNS e conexões de socket para impedir acesso acidental à rede. Não usam credenciais reais nem leem a sessão do usuário. Cobrem extração, filtros, paginação, limites, retomada, transações, idempotência, tratamento de falhas, proteção de sessão, CLI, logs estruturados, privacidade com `--verbose`, contagem apenas de páginas confirmadas e interpretação de discovery parcial. A consulta individual cobre preservação de todos os checkpoints, mensagens ausentes/sem vídeo, resposta de canal/ID incorreto, timeout, repetição idempotente e conflitos de opções. A extração hierárquica cobre o cabeçalho genérico, estrutura incompleta/ambígua e nomes seguros com limite de comprimento.

Os testes de download cobrem layout e filtros de módulos, migração preservando checkpoints, prévia sem rede/escrita, bytes e SHA-256, retomada após interrupção, referências expiradas, timeout/rede/FloodWait, conflitos de arquivos, limites de tamanho, publicação atômica, recuperação após falha na publicação e concorrência limitada.

A aplicação utiliza conexão de sessão, autenticação explícita por `auth`, consulta de autorização/conta, resolução do canal, leitura de diálogos quando o canal não está no cache leitura de histórico/mensagens individuais e transferência explícita de documentos. `auth` solicita ao Telegram o código de login e confirma código/senha; isso não envia mensagens ao canal. Não utiliza Telegram Web ou Bot API, não marca mensagens como lidas, não envia/encaminha/edita/apaga mensagens e não faz uploads. Os testes de login também usam mocks, cobrindo 2FA, sessão existente, erros e privacidade, sem autenticar contas reais.

Referências: [paginação e cliente Telethon](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.messages.MessageMethods.iter_messages), [sessões](https://docs.telethon.dev/en/stable/concepts/sessions.html), [erros RPC](https://docs.telethon.dev/en/stable/concepts/errors.html).

Referência de download: [streaming e offset no Telethon](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.iter_download).
