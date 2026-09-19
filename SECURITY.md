# Segurança

Este projeto acessa somente canais do Telegram para os quais a equipe tem
autorização. O acesso usa uma conta de usuário via MTProto/Telethon e os dados
baixados permanecem locais.

Não abra issues ou pull requests com `.env`, arquivos `.session`, manifestos
SQLite, legendas, vídeos, logs de autenticação, IDs privados de canais ou
qualquer outro dado de operação. Esses arquivos já são ignorados pelo Git, mas
confirme o staging com `git status --short` antes de cada commit.

Para relatar uma vulnerabilidade, use um canal privado administrado pelos
responsáveis deste repositório. Inclua apenas a descrição mínima necessária e
não envie credenciais, sessões ou arquivos baixados. Não execute `auth` nem
downloads para reproduzir um problema sem autorização explícita para o canal.
