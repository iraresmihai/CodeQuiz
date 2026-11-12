import java.io.*;
import java.lang.reflect.*;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/**
 * Runs the tests of one submission. Started by server.py as
 *     java -cp runner/build QuizRunner <dir with compiled submission + tests> <manifest>
 *
 * Manifest: one test per line, tab separated:  kind  index  class  stdinFile  timeoutMs
 *   kind "output": run main() of class with the stdin file, report what it printed
 *   kind "code":   run the static method run() of the generated test class
 *
 * Every test gets fresh class loaders (so static fields start over), its own thread
 * with a big stack, a time limit and its own System.in / System.out.
 *
 * Output lines (on the real stdout):
 *   ##START  index
 *   ##R      index  status  base64(printed output)  base64(message)
 *   ##END
 * status: ok | fail (a check failed) | error (exception) | timeout
 */
public class QuizRunner {
    static final PrintStream REAL = new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8);
    static final int CAP = 1 << 20;   // keep at most 1 MB of printed output per test

    /** Where the current thread's System.out goes and where its System.in comes from. */
    static final class Capture {
        final ByteArrayOutputStream out = new ByteArrayOutputStream();
        InputStream in = InputStream.nullInputStream();
        boolean truncated;

        synchronized void write(byte[] b, int off, int len) {
            int room = CAP - out.size();
            if (room <= 0) { truncated = true; return; }
            if (len > room) { len = room; truncated = true; }
            out.write(b, off, len);
        }

        synchronized String text() {
            return out.toString(StandardCharsets.UTF_8) + (truncated ? "\n... (output cut off after 1 MB)" : "");
        }
    }

    static final InheritableThreadLocal<Capture> CURRENT = new InheritableThreadLocal<>();

    static final class RoutingOut extends OutputStream {
        @Override public void write(int b) { write(new byte[]{(byte) b}, 0, 1); }
        @Override public void write(byte[] b, int off, int len) {
            Capture c = CURRENT.get();
            if (c != null) c.write(b, off, len);
        }
    }

    static final class RoutingIn extends InputStream {
        InputStream src() { Capture c = CURRENT.get(); return c == null ? InputStream.nullInputStream() : c.in; }
        @Override public int read() throws IOException { return src().read(); }
        @Override public int read(byte[] b, int off, int len) throws IOException { return src().read(b, off, len); }
        @Override public int available() throws IOException { return src().available(); }
    }

    public static void main(String[] args) throws Exception {
        Path dir = Path.of(args[0]);
        List<String> lines = Files.readAllLines(Path.of(args[1]), StandardCharsets.UTF_8);
        System.setOut(new PrintStream(new RoutingOut(), true, StandardCharsets.UTF_8));
        System.setErr(new PrintStream(OutputStream.nullOutputStream(), true, StandardCharsets.UTF_8));
        System.setIn(new RoutingIn());
        URL[] urls = {dir.toUri().toURL()};
        for (String line : lines) {
            if (line.isBlank()) continue;
            String[] p = line.split("\t", -1);
            String idx = p[1];
            REAL.println("##START\t" + idx);
            byte[] stdin = p[3].isEmpty() ? new byte[0] : Files.readAllBytes(dir.resolve(p[3]));
            String[] r = runOne(urls, p[0], p[2], stdin, Long.parseLong(p[4]));
            REAL.println("##R\t" + idx + "\t" + r[0] + "\t" + b64(r[1]) + "\t" + b64(r[2]));
        }
        REAL.println("##END");
        REAL.flush();
        Runtime.getRuntime().halt(0);   // also stops tests that are still running after their time limit
    }

    static String b64(String s) {
        return Base64.getEncoder().encodeToString(s.getBytes(StandardCharsets.UTF_8));
    }

    /** Returns {status, printed output, message}. */
    static String[] runOne(URL[] urls, String kind, String cls, byte[] stdin, long timeoutMs) throws InterruptedException {
        Capture cap = new Capture();
        cap.in = new ByteArrayInputStream(stdin);
        String[] res = {"ok", ""};
        URLClassLoader loader = new URLClassLoader(urls, QuizRunner.class.getClassLoader());
        Thread t = new Thread(null, () -> {
            CURRENT.set(cap);
            try {
                Class<?> c = Class.forName(cls, true, loader);
                if (kind.equals("output")) runMain(c);
                else {
                    Method m = c.getDeclaredMethod("run");
                    m.setAccessible(true);
                    m.invoke(null);
                }
            } catch (InvocationTargetException e) {
                describe(e.getCause(), res);
            } catch (ExceptionInInitializerError e) {
                describe(e.getCause() == null ? e : e.getCause(), res);
            } catch (ClassNotFoundException e) {
                res[0] = "error"; res[1] = "class " + cls + " not found (check the class name)";
            } catch (NoSuchMethodException e) {
                res[0] = "error"; res[1] = e.getMessage();
            } catch (Throwable e) {
                describe(e, res);
            }
        }, "test", 512L << 20);
        t.setDaemon(true);
        t.start();
        t.join(timeoutMs);
        if (t.isAlive()) {
            res[0] = "timeout";
            res[1] = "too slow (over " + (timeoutMs / 1000.0) + " s): endless loop, or not efficient enough?";
        }
        return new String[]{res[0], cap.text(), res[1]};
    }

    /** main(String[]) or main(), static or not (Java 25 allows instance main methods). */
    static void runMain(Class<?> c) throws Throwable {
        Method m = null;
        boolean withArgs = false;
        for (Class<?> k = c; k != null && m == null; k = k.getSuperclass()) {
            try { m = k.getDeclaredMethod("main", String[].class); withArgs = true; }
            catch (NoSuchMethodException e) {
                try { m = k.getDeclaredMethod("main"); } catch (NoSuchMethodException ignored) { }
            }
        }
        if (m == null) throw new NoSuchMethodException("no main method in class " + c.getSimpleName());
        m.setAccessible(true);
        Object self = null;
        if (!Modifier.isStatic(m.getModifiers())) {
            Constructor<?> k = c.getDeclaredConstructor();
            k.setAccessible(true);
            self = k.newInstance();
        }
        if (withArgs) m.invoke(self, (Object) new String[0]);
        else m.invoke(self);
    }

    static void describe(Throwable x, String[] res) {
        if (x instanceof CQ.Failure) { res[0] = "fail"; res[1] = x.getMessage(); return; }
        res[0] = "error";
        if (x instanceof StackOverflowError) { res[1] = "StackOverflowError: endless recursion?"; return; }
        if (x instanceof OutOfMemoryError) { res[1] = "OutOfMemoryError: uses too much memory"; return; }
        String where = "";
        for (StackTraceElement e : x.getStackTrace()) {
            String c = e.getClassName();
            if (c.startsWith("java.") || c.startsWith("jdk.") || c.startsWith("sun.") || c.equals("QuizRunner")
                    || c.startsWith("CQ") || c.startsWith("QuizRunner$")) continue;
            where = " (at " + e.getClassName() + "." + e.getMethodName() + (e.getLineNumber() > 0 ? ", line " + e.getLineNumber() : "") + ")";
            break;
        }
        res[1] = x.getClass().getSimpleName() + (x.getMessage() != null ? ": " + x.getMessage() : "") + where;
    }
}
