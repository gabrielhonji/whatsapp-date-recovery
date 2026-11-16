import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Base64;

/** Runs several Android content commands in one app_process invocation. */
public final class WhatsAppDatesMedia {
    private static final File BASE = new File(
        "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"
    );
    private static final String IMAGES = "content://media/external_primary/images/media";

    public static void main(String[] args) throws Exception {
        Method content = Class.forName("com.android.commands.content.Content")
            .getMethod("main", String[].class);
        PrintStream protocol = System.out;
        PrintStream errors = System.err;
        BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        String line;
        while ((line = input.readLine()) != null) {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            ByteArrayOutputStream diagnostics = new ByteArrayOutputStream();
            int status = 0;
            try (PrintStream out = new PrintStream(output, true, "UTF-8");
                 PrintStream err = new PrintStream(diagnostics, true, "UTF-8")) {
                System.setOut(out);
                System.setErr(err);
                execute(content, line.split("\t", -1));
            } catch (Exception failure) {
                diagnostics.write(failure.toString().getBytes(StandardCharsets.UTF_8));
            } finally {
                System.setOut(protocol);
                System.setErr(errors);
            }
            if (diagnostics.size() > 0) {
                status = 1;
                output = diagnostics;
            }
            protocol.print(status);
            protocol.write(0);
            protocol.write(output.toByteArray());
            protocol.write(0);
            protocol.flush();
        }
    }

    private static void execute(Method content, String[] fields) throws Exception {
        String[] command;
        if (fields[0].equals("scan") && fields.length == 2) {
            File image = new File(decode(fields[1]));
            if (!BASE.equals(image.getParentFile()) || image.getName().startsWith(".")
                || !image.isFile() || Files.isSymbolicLink(image.toPath())) {
                throw new IllegalArgumentException("Invalid visible image");
            }
            for (File dir = BASE; dir.getParentFile() != null; dir = dir.getParentFile()) {
                File marker = new File(dir, ".nomedia");
                if (marker.exists() || Files.isSymbolicLink(marker.toPath())) {
                    System.out.println("hidden");
                    return;
                }
            }
            command = new String[] {
                "call", "--uri", "content://media", "--method", "scan_file", "--arg", image.getPath()
            };
        } else if (fields[0].equals("query") && fields.length == 3) {
            command = new String[] {
                "query", "--uri", IMAGES, "--projection", decode(fields[1]), "--where", decode(fields[2])
            };
        } else {
            throw new IllegalArgumentException("Invalid command");
        }
        content.invoke(null, (Object) command);
    }

    private static String decode(String value) {
        return new String(Base64.getDecoder().decode(value), StandardCharsets.UTF_8);
    }
}
