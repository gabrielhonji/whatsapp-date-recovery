# Recompilar o auxiliar Android

O projeto inclui `android/whatsapp-dates-media.dex`, compilado de `android/WhatsAppDatesMedia.java`. Quem vai executar a ferramenta só precisa do DEX incluído. Para recompilar, instale um JDK e o [D8 do Android SDK Build Tools](https://developer.android.com/tools/d8) e disponibilize `javac` e `d8` no `PATH`.

No PowerShell, a partir da raiz do repositório:

```powershell
New-Item -ItemType Directory -Path .build-android -Force | Out-Null
javac --release 8 -d .build-android .\android\WhatsAppDatesMedia.java
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }
d8 --release --min-api 26 --output .build-android .\.build-android\WhatsAppDatesMedia.class
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }
Copy-Item .\.build-android\classes.dex .\android\whatsapp-dates-media.dex -Force
```

O DEX incluído foi compilado com JDK 21 e D8 8.5.35. O código-fonte usa a interface do comando `content` do Android. O script envia o auxiliar para um diretório temporário do aparelho via ADB, verifica se funciona antes de gravar arquivos visíveis e usa um processo Android por grupo. Mudanças de compatibilidade precisam de teste em aparelho real, pois os testes automatizados simulam o ADB.
