import java.lang.reflect.Array;
import java.nio.charset.StandardCharsets;
import java.io.ByteArrayInputStream;
import java.util.*;

/**
 * Helpers for "code" tests. Every code test is a class that extends CQ, so these
 * methods can be called directly:
 *
 *     check(Solution.isPrime(7), true);
 *     check(Solution.max(new int[]{1, 5, 3}), 5);
 *     check(Solution.average(new int[]{1, 2}), 1.5, 1e-9);
 *     check(x > 0, "x should be positive");
 *     String printed = output(() -> Solution.printAll(list));
 *     checkOutput(() -> Solution.printAll(list), "1\n2\n3");
 *     checkOutputAnyOrder(() -> Solution.printAll(set), "1\n2\n3");
 *     String printed = output("5 7\n", () -> Solution.main(new String[0]));   // with stdin
 *     fail("custom message");
 */
public class CQ {
    public static final class Failure extends AssertionError {
        public Failure(String msg) { super(msg); }
    }

    public interface Code { void run() throws Throwable; }

    public static void fail(String message) { throw new Failure(message); }

    public static void check(boolean condition) { if (!condition) fail("check failed"); }

    public static void check(boolean condition, String message) { if (!condition) fail(message); }

    /** actual must equal expected. Numbers compare by value (2 == 2L), arrays and lists element by element. */
    public static void check(Object actual, Object expected) {
        if (!same(actual, expected, 1e-9)) fail("expected " + show(expected) + ", got " + show(actual));
    }

    public static void check(double actual, double expected, double tolerance) {
        if (!(Math.abs(actual - expected) <= tolerance)) fail("expected " + expected + " (± " + tolerance + "), got " + actual);
    }

    /** Runs the code and returns what it printed. */
    public static String output(Code code) throws Throwable { return output(null, code); }

    /** Runs the code with the given text as System.in and returns what it printed. */
    public static String output(String stdin, Code code) throws Throwable {
        QuizRunner.Capture outer = QuizRunner.CURRENT.get();
        QuizRunner.Capture inner = new QuizRunner.Capture();
        inner.in = stdin == null ? (outer != null ? outer.in : java.io.InputStream.nullInputStream())
                                 : new ByteArrayInputStream(stdin.getBytes(StandardCharsets.UTF_8));
        QuizRunner.CURRENT.set(inner);
        try {
            code.run();
        } finally {
            QuizRunner.CURRENT.set(outer);
        }
        return inner.text();
    }

    /** Printed lines must match (trailing spaces and empty lines at the end don't matter). */
    public static void checkOutput(Code code, String expected) throws Throwable {
        List<String> got = lines(output(code)), want = lines(expected);
        if (!got.equals(want)) fail("expected output\n" + String.join("\n", want) + "\ngot\n" + String.join("\n", got));
    }

    /** Same lines in any order (spaces at the start/end of a line and double spaces don't matter, empty lines are ignored). */
    public static void checkOutputAnyOrder(Code code, String expected) throws Throwable {
        List<String> got = words(lines(output(code))), want = words(lines(expected));
        List<String> a = new ArrayList<>(got), b = new ArrayList<>(want);
        Collections.sort(a);
        Collections.sort(b);
        if (!a.equals(b)) fail("expected these lines (any order)\n" + String.join("\n", want) + "\ngot\n" + String.join("\n", got));
    }

    /** The lines of a text, without trailing spaces and without empty lines at the end. */
    public static List<String> lines(String text) {
        List<String> out = new ArrayList<>();
        for (String l : text.split("\\R", -1)) out.add(l.stripTrailing());
        while (!out.isEmpty() && out.get(out.size() - 1).isEmpty()) out.remove(out.size() - 1);
        return out;
    }

    static List<String> words(List<String> lines) {
        List<String> out = new ArrayList<>();
        for (String l : lines) {
            String w = l.strip().replaceAll("\\s+", " ");
            if (!w.isEmpty()) out.add(w);
        }
        return out;
    }

    // ---------- comparing and printing values ----------

    static boolean integral(Object o) {
        return o instanceof Integer || o instanceof Long || o instanceof Short || o instanceof Byte
                || o instanceof java.math.BigInteger;
    }

    static boolean same(Object a, Object e, double eps) {
        if (a == e) return true;
        if (a == null || e == null) return false;
        if (a instanceof Number x && e instanceof Number y) {
            if (integral(x) && integral(y)) return new java.math.BigInteger(x.toString()).equals(new java.math.BigInteger(y.toString()));
            double p = x.doubleValue(), q = y.doubleValue();
            return p == q || Math.abs(p - q) <= eps * Math.max(1, Math.abs(q));
        }
        if (a instanceof Character c && e instanceof Character d) return c.equals(d);
        if (a.getClass().isArray() && e.getClass().isArray()) {
            int n = Array.getLength(a);
            if (n != Array.getLength(e)) return false;
            for (int i = 0; i < n; i++) if (!same(Array.get(a, i), Array.get(e, i), eps)) return false;
            return true;
        }
        if (a instanceof List<?> x && e instanceof List<?> y) {
            if (x.size() != y.size()) return false;
            for (int i = 0; i < x.size(); i++) if (!same(x.get(i), y.get(i), eps)) return false;
            return true;
        }
        if (a.getClass().isArray() && e instanceof List<?> y) return same(toList(a), y, eps);
        if (e.getClass().isArray() && a instanceof List<?> x) return same(x, toList(e), eps);
        return a.equals(e);
    }

    static List<Object> toList(Object arr) {
        List<Object> l = new ArrayList<>();
        for (int i = 0; i < Array.getLength(arr); i++) l.add(Array.get(arr, i));
        return l;
    }

    static String show(Object o) {
        if (o == null) return "null";
        if (o instanceof String s) return "\"" + s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\"", "\\\"") + "\"";
        if (o instanceof Character c) return "'" + c + "'";
        if (o instanceof Long l) return l.toString();
        if (o.getClass().isArray()) {
            StringBuilder sb = new StringBuilder("[");
            int n = Array.getLength(o);
            for (int i = 0; i < n; i++) {
                if (i > 0) sb.append(", ");
                if (i == 50) { sb.append("... (").append(n).append(" elements)"); break; }
                sb.append(show(Array.get(o, i)));
            }
            return sb.append("]").toString();
        }
        String s = String.valueOf(o);
        return s.length() > 500 ? s.substring(0, 500) + "..." : s;
    }
}
