package sh.miles.pressmc.data;

import java.util.Map;

public record JavaType(String jvm) {

    private static final Map<String, Character> PRIMITIVES = Map.of(
        "boolean", 'Z',
        "byte", 'B',
        "char", 'C',
        "short", 'S',
        "int", 'I',
        "long", 'J',
        "float", 'F',
        "double", 'D',
        "void", 'V'
    );

    /**
     * Creates a JavaType from a Java source style type name as written in deprecated-data.csv, e.g. {@code int},
     * {@code java.lang.Class[]} or {@code org.bukkit.event.player.PlayerPreLoginEvent.Result}.
     * <p>
     * The CSV writes nested types with dots, so the package/class boundary is inferred from Java naming conventions:
     * the first segment starting with an uppercase letter is the outermost class, and every segment after it is a
     * nested class ({@code $} in JVM form).
     *
     * @param source the source style type name
     * @return the type, whose {@link #jvm()} is the JVM descriptor, e.g. {@code [Ljava/lang/Class;}
     * @throws IllegalArgumentException if {@code source} is blank
     */
    public static JavaType fromSource(final String source) {
        String name = source.strip();
        final StringBuilder jvm = new StringBuilder();
        while (true) {
            if (name.endsWith("[]")) {
                name = name.substring(0, name.length() - 2).strip();
            } else if (name.endsWith("...")) {
                name = name.substring(0, name.length() - 3).strip();
            } else {
                break;
            }
            jvm.append('[');
        }
        if (name.isEmpty()) {
            throw new IllegalArgumentException("Not a type: '" + source + "'");
        }

        final Character primitive = PRIMITIVES.get(name);
        if (primitive != null) {
            return new JavaType(jvm.append(primitive).toString());
        }

        jvm.append('L');
        boolean inClass = false;
        final String[] segments = name.split("\\.");
        for (int i = 0; i < segments.length; i++) {
            if (i > 0) {
                jvm.append(inClass ? '$' : '/');
            }
            jvm.append(segments[i]);
            if (!segments[i].isEmpty() && Character.isUpperCase(segments[i].charAt(0))) {
                inClass = true;
            }
        }
        return new JavaType(jvm.append(';').toString());
    }

    /**
     * The inverse of {@link #fromSource(String)}: {@code [Ljava/lang/Class;} becomes {@code java.lang.Class[]}, and
     * {@code Lorg/bukkit/event/player/PlayerPreLoginEvent$Result;} becomes
     * {@code org.bukkit.event.player.PlayerPreLoginEvent.Result}.
     * <p>
     * Also recovers member paths that were passed through {@code fromSource}, e.g. {@code LgetData(int, int, int);}
     * becomes {@code getData(int, int, int)}: CSV text never contains {@code /} or {@code $}, so the conversion is
     * exactly reversible.
     */
    public String toSource() {
        int dimensions = 0;
        while (dimensions < jvm.length() && jvm.charAt(dimensions) == '[') {
            dimensions++;
        }
        final String element = jvm.substring(dimensions);
        String name = null;
        if (element.length() == 1) {
            for (final Map.Entry<String, Character> primitive : PRIMITIVES.entrySet()) {
                if (primitive.getValue() == element.charAt(0)) {
                    name = primitive.getKey();
                }
            }
        }
        if (name == null) {
            final String body = element.length() > 2 && element.charAt(0) == 'L' && element.charAt(element.length() - 1) == ';'
                ? element.substring(1, element.length() - 1)
                : element;
            name = body.replace('/', '.').replace('$', '.');
        }
        return name + "[]".repeat(dimensions);
    }
}
