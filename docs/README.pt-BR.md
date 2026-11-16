# WhatsApp Date Recovery

[English](../README.md)

Se as fotos e os vídeos do WhatsApp ficaram com a data da cópia ou da restauração, esta ferramenta para Windows ajuda a recuperar a ordem das mensagens na galeria. Ela associa os nomes dos arquivos JPEG, MP4 e MOV às datas das mensagens em um banco WhatsApp já descriptografado. Em seguida, pode ajustar a data de modificação e atualizar o índice da mídia visível.

A data da mensagem pode ser diferente da data em que a mídia foi gravada. A ferramenta funciona localmente, mantém intactos o conteúdo dos arquivos, os metadados internos e os marcadores `.nomedia`, e pula arquivos com nomes duplicados ou datas conflitantes.

## O que você precisa

- Windows 11 com [Python 3.12](https://www.python.org/downloads/) e [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools).
- Celular Android conectado por USB, com **Developer options → USB debugging** ativado. O fluxo foi testado na Samsung Gallery em um Samsung Galaxy. Outros aparelhos e galerias podem se comportar de forma diferente.
- Um **`msgstore.db` já descriptografado**, com as tabelas `message` e `message_media` e os registros das mídias que você quer ajustar. A ferramenta precisa desse arquivo para associar cada JPEG a uma data. Extração e descriptografia do banco ficam fora do escopo. Arquivos `msgstore.db.crypt*` e conversas exportadas em texto são incompatíveis.

Baixe e extraia o projeto inteiro, incluindo a pasta `android`. Abra o **PowerShell** na pasta extraída, ajuste os dois caminhos abaixo e conecte o celular desbloqueado.

```powershell
$db = 'C:\caminho\para\msgstore.db'
$adb = 'C:\caminho\para\platform-tools\adb.exe'
& $adb devices -l
```

Se aparecer **Allow USB debugging** no celular, aceite. O aparelho deve aparecer com o estado `device`. Se houver mais de um aparelho autorizado, adicione `--serial SERIAL` aos comandos da ferramenta. Caso `py` esteja indisponível, use o caminho completo do executável do Python 3.12 no lugar de `py -3.12`.

## 1. Faça uma prévia

```powershell
py -3.12 .\whatsapp_dates.py --db $db --adb $adb
```

A prévia não altera o celular. Ela mostra os arquivos encontrados, os ajustes a fazer e as exclusões. Também grava um CSV local com caminhos e datas em UTC. Você pode escolher um arquivo novo com `--report 'C:\caminho\para\previa.csv'`. Por padrão, a prévia, a aplicação e o rollback incluem fotos e vídeos. Adicione `--media images` para somente fotos ou `--media videos` para somente vídeos.

## 2. Teste em até três arquivos

```powershell
py -3.12 .\whatsapp_dates.py --db $db --adb $adb --apply --limit 3
```

Anote os arquivos exibidos. O comando verifica o horário gravado e encerra a **Gallery** ao final se houve reindexação de mídias visíveis. Abra a Gallery e confira a posição dos arquivos. Se rodar a prévia de novo, os arquivos ajustados devem aparecer como `unchanged`.

## 3. Aplique à coleção

```powershell
py -3.12 .\whatsapp_dates.py --db $db --adb $adb --apply
```

A barra estima o tempo restante da etapa atual. Se precisar parar, use **Ctrl+C**. Para retomar, repita o comando com o mesmo aparelho e o mesmo journal. Os arquivos concluídos são pulados.

O arquivo `whatsapp-dates.journal.sqlite3` fica na pasta atual, salvo se você escolher outro caminho com `--journal`. Guarde-o em local privado se puder precisar desfazer as alterações.

## 4. Desfaça se necessário

```powershell
py -3.12 .\whatsapp_dates.py --adb $adb --rollback
```

O rollback restaura as datas de modificação de todos os registros ativos no journal selecionado. Se você só fez o teste inicial, isso corresponde a até três arquivos. Use o mesmo `--journal` caso tenha escolhido um caminho personalizado. O journal contém datas e dados da operação, por isso não permite recuperar o conteúdo dos arquivos.

## Escopo e privacidade

- A busca inclui JPEGs em `WhatsApp Images` e vídeos MP4/MOV em `WhatsApp Video`, incluindo as subpastas `Private` e `Sent` de cada uma. Nessas subpastas a ferramenta só altera a data de modificação. A reindexação explícita é restrita às mídias visíveis elegíveis de cada pasta principal.
- O auxiliar Android roda temporariamente via ADB para agrupar as chamadas do MediaStore. Ele funciona sem root ou instalação de app. Mantenha o DEX na pasta `android`.
- Se quiser fazer um backup opcional, escolha destinos locais separados para fotos e vídeos:

  ```powershell
  & $adb pull -a '/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images' 'C:\caminho\backup-das-fotos'
  & $adb pull -a '/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video' 'C:\caminho\backup-dos-videos'
  ```

- Ao reportar um problema, informe o modelo do aparelho, a versão do Android e o erro com dados pessoais removidos. Mantenha o banco, as fotos, o journal, o CSV e o serial do aparelho em privado.

Para executar os testes sintéticos, rode `py -3.12 -m unittest -v`. Consulte também [como recompilar o auxiliar Android](android-helper.md).

Projeto independente sob a [licença MIT](../LICENSE). WhatsApp e Samsung são marcas de seus respectivos proprietários.
