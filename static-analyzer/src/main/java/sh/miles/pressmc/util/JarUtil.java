package sh.miles.pressmc.util;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Path;
import java.util.Enumeration;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

public final class JarUtil {

    private JarUtil() {
    }

    @FunctionalInterface
    public interface ClassConsumer {
        /**
         * @param entryName the jar entry, e.g. {@code com/example/MyPlugin.class}
         * @param classFile the class file bytes
         */
        void accept(String entryName, byte[] classFile);
    }

    /**
     * Calls {@code consumer} for every class file in a jar.
     * <p>
     * Skips {@code module-info.class}, and the extra per-Java-version copies multi-release jars keep under
     * {@code META-INF/versions/}, so each class is only seen once.
     */
    public static void forEachClass(final Path jar, final ClassConsumer consumer) throws IOException {
        try (JarFile jarFile = new JarFile(jar.toFile())) {
            final Enumeration<JarEntry> entries = jarFile.entries();
            while (entries.hasMoreElements()) {
                final JarEntry entry = entries.nextElement();
                final String name = entry.getName();
                if (entry.isDirectory() || !name.endsWith(".class") || name.endsWith("module-info.class")
                    || name.startsWith("META-INF/versions/")) {
                    continue;
                }
                // technically could overflow memory but we will assume single classes are small
                final byte[] classFile;
                try (InputStream in = jarFile.getInputStream(entry)) {
                    classFile = in.readAllBytes();
                }
                consumer.accept(name, classFile);
            }
        }
    }
}
