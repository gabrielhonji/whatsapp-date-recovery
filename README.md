# WhatsApp Date Recovery

[Leia em português](docs/README.pt-BR.md)

If copied or restored WhatsApp photos all show the date they were copied, this Windows command-line tool can put them back in message-date order. It matches JPEG filenames on your Android phone with message timestamps in an existing decrypted WhatsApp database, updates their filesystem modification times and refreshes visible images in Android's MediaStore.

The dates come from the WhatsApp messages. They can differ from the dates the photos were taken. The tool works locally and leaves JPEG contents, EXIF, folders and `.nomedia` markers intact. It skips conflicting dates and duplicate filenames.

## What you need

- A Windows 11 PC with [Python 3.12](https://www.python.org/downloads/) and [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools).
- An Android phone connected by USB, with **Developer options → USB debugging** enabled. The workflow was tested on Samsung Gallery on a Samsung Galaxy. Other phones and gallery apps may behave differently.
- An **already decrypted** `msgstore.db` that covers the photos you want to fix. The tool expects the `message` and `message_media` tables. A WhatsApp chat text export and encrypted `msgstore.db.crypt*` backups are unsupported. Database extraction and decryption are outside this tool's scope.

Download and extract the entire project, including the `android` folder. Open **PowerShell** in the extracted folder, change the two example paths below, and connect an unlocked phone.

```powershell
$db = 'C:\path\to\msgstore.db'
$adb = 'C:\path\to\platform-tools\adb.exe'
& $adb devices -l
```

Accept **Allow USB debugging** on the phone when prompted. Continue when the device status is `device`. If several devices are authorized, append `--serial SERIAL` to each tool command. If `py` is unavailable, substitute the full path to your Python 3.12 executable for `py -3.12`.

## 1. Preview

```powershell
py -3.12 .\whatsapp_dates.py --db $db --adb $adb
```

The default preview makes no changes to the phone. It reports matches, conflicts and pending adjustments, and writes a local CSV with file paths and UTC timestamps. Use `--report 'C:\path\to\new-preview.csv'` to choose a new output file.

The terminal output is in Brazilian Portuguese. **Precisam de alteração** means files that need adjustment, **Já corretos** means files that already match, and **Erros: 0** means the operation reported no errors. The CSV uses English status values such as `needs_change`, `unchanged` and `conflict`.

## 2. Test on up to three photos

```powershell
py -3.12 .\whatsapp_dates.py --db $db --adb $adb --apply --limit 3
```

Note the listed files. After the command finishes, reopen **Gallery** and check those photos. Samsung Gallery is closed once after batches that reindex visible images. Run another preview to confirm that the test files are listed as `unchanged`.

## 3. Apply to the collection

```powershell
py -3.12 .\whatsapp_dates.py --db $db --adb $adb --apply
```

The progress bar estimates time remaining for the current stage. To pause, press **Ctrl+C**. Use the same command, phone and journal to resume. Successfully completed files are skipped.

The journal is `whatsapp-dates.journal.sqlite3` in the current directory unless you specify `--journal`. Keep it private and retain it if you might need to undo the changes.

## 4. Roll back

```powershell
py -3.12 .\whatsapp_dates.py --adb $adb --rollback
```

Rollback restores the modification times recorded in the selected journal. With the original test-only journal, that means up to three files. After a full run, it applies to all active records in that journal. Use the same `--journal` path if you chose a custom one. The journal stores timestamps and operation metadata, so it cannot restore file contents.

## Scope and privacy

- Only JPEGs in WhatsApp's `WhatsApp Images`, `Private` and `Sent` folders are considered. Files in `Private` and `Sent` receive timestamp adjustments only. Explicit reindexing is limited to eligible visible images in the main folder.
- A small, bundled Android helper runs temporarily through ADB to group MediaStore calls. It requires neither an installed Android app nor root. Keep its DEX file beside the source in `android/`.
- To optionally back up the photos first, choose a separate local destination and run:

  ```powershell
  & $adb pull -a '/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images' 'C:\path\to\photo-backup'
  ```

- When reporting an issue, share the device model, Android version and a redacted error. Keep your database, photos, journal, CSV and device serial private.

## Development

Run `py -3.12 -m unittest -v` for the synthetic SQLite and mocked ADB tests. See [the Android helper build instructions](docs/android-helper.md) to rebuild the bundled DEX.

Released under the [MIT License](LICENSE). Independent project. WhatsApp and Samsung are trademarks of their respective owners.
