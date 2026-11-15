package sh.miles.pressmc;

import org.objectweb.asm.ClassReader;
import sh.miles.pressmc.data.Deprecated;
import sh.miles.pressmc.util.JarUtil;
import sh.miles.pressmc.walker.ClassHierarchy;
import sh.miles.pressmc.walker.ClassUsage;
import sh.miles.pressmc.walker.Inspector;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.file.FileVisitOption;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

public class Main {

    private static final String HELP = """
        static-analyzer <deprecated-data-file> <api-jar> <plugins-folder> [output-folder]

          deprecated-data-file  CSV of deprecated API (type,path,owner), e.g. report/data/deprecated-data.csv
          api-jar               the API the plugins compile against (e.g. paper-api-26.3.jar, or a classes folder),
                                used to also catch deprecated members called through a subtype
          plugins-folder        folder containing the plugin jars to analyze (not searched recursively)
          output-folder         optional; writes usage-by-member.csv and usage-by-plugin.csv here""";

    static void main(String[] args) {
        if (args.length != 3 && args.length != 4) {
            System.err.println("invalid arguments provided");
            System.err.println(HELP);
            System.exit(2);
        }

        Path deprecatedFile = Path.of(args[0]);
        if (!Files.isRegularFile(deprecatedFile)) {
            fail("deprecatedFile %s is not a file", deprecatedFile);
        }

        Path apiJar = Path.of(args[1]);
        if (!Files.exists(apiJar)) {
            fail("api-jar %s does not exist", apiJar);
        }

        Path pluginsFolder = Path.of(args[2]);
        if (!Files.isDirectory(pluginsFolder)) {
            fail("pluginsFolder %s is not a folder", pluginsFolder);
        }

        Path outputFolder = args.length == 4 ? Path.of(args[3]) : null;

        List<Deprecated> deprecateds = Deprecated.read(deprecatedFile);
        if (deprecateds.isEmpty()) {
            fail("deprecatedFile %s is an empty file", deprecatedFile);
        }

        ClassHierarchy hierarchy;
        try {
            hierarchy = ClassHierarchy.load(apiJar);
        } catch (IOException e) {
            fail("could not read api-jar %s: %s", apiJar, e.getMessage());
            return;
        }
        Inspector inspector = new Inspector(deprecateds, hierarchy);
        System.out.printf("Loaded %d deprecated entries and %d API classes%n", deprecateds.size(), hierarchy.size());

        List<Path> jars;
        try (Stream<Path> files = Files.walk(pluginsFolder, 1, FileVisitOption.FOLLOW_LINKS)) {
            jars = files.filter(Files::isRegularFile)
                .filter((path) -> path.getFileName().toString().endsWith(".jar"))
                .sorted()
                .toList();
        } catch (IOException e) {
            fail("could not list %s: %s", pluginsFolder, e.getMessage());
            return;
        }
        if (jars.isEmpty()) {
            fail("no jars found in %s", pluginsFolder);
        }

        // plugin file name -> per-entry use counts for that plugin
        Map<String, Map<Deprecated, Long>> byPlugin = new LinkedHashMap<>();
        // plugin file name -> class name -> per-entry use counts made by that class (non-zero only)
        Map<String, Map<String, Map<Deprecated, Long>>> byClass = new LinkedHashMap<>();
        int jarsBundlingApi = 0;
        for (Path jar : jars) {
            String plugin = jar.getFileName().toString();
            try {
                JarResult result = analyzeJar(jar, inspector, hierarchy);
                Map<Deprecated, Long> counts = new LinkedHashMap<>();
                result.byClass().values().forEach(classCounts -> classCounts.forEach((deprecated, count) -> counts.merge(deprecated, count, Long::sum)));
                byPlugin.put(plugin, counts);
                byClass.put(plugin, result.byClass());
                long uses = counts.values().stream().mapToLong(Long::longValue).sum();
                if (result.bundledApi() > 0) {
                    jarsBundlingApi++;
                }
                System.out.printf("Processed %s: %d classes, %d deprecated uses of %d entries%s%s%n",
                    plugin, result.classes(), uses, counts.size(),
                    result.bundledApi() > 0 ? " (" + result.bundledApi() + " bundled API classes skipped)" : "",
                    result.unreadable() > 0 ? " (" + result.unreadable() + " unreadable classes skipped)" : "");
            } catch (IOException e) {
                inspector.countsAndReset();
                System.err.printf("Skipping %s: %s%n", plugin, e.getMessage());
            }
        }
        if (jarsBundlingApi > 0) {
            System.out.printf("%n%d plugins bundle copies of API classes; references made inside those copies were not counted%n", jarsBundlingApi);
        }

        List<Usage> usages = summarize(deprecateds, byPlugin);
        printSummary(usages, byPlugin.size());

        if (outputFolder != null) {
            try {
                writeCsvs(outputFolder, usages, byPlugin, byClass);
                System.out.printf("%nWrote usage-by-member.csv, usage-by-plugin.csv and usage-by-class.csv to %s%n", outputFolder);
            } catch (IOException e) {
                fail("could not write output to %s: %s", outputFolder, e.getMessage());
            }
        }
    }

    /**
     * @param classes classes analyzed
     * @param unreadable classes ASM couldn't parse
     * @param bundledApi classes skipped because they are copies of API classes bundled into the plugin jar
     * @param byClass class name -> non-zero per-entry use counts made by that class
     */
    public record JarResult(int classes, int unreadable, int bundledApi, Map<String, Map<Deprecated, Long>> byClass) {
    }

    /**
     * Feeds every class in the jar through the inspector, collecting counts per class. Classes ASM can't parse (e.g.
     * compiled for a newer Java version than this ASM supports) are skipped and counted.
     * <p>
     * Classes with the same name as an API class are copies of the API bundled into the plugin jar by mistake (a
     * shaded dependency that should have been {@code compileOnly}). The server loads its own API classes instead, so
     * references made inside those copies are not the plugin's usage and are skipped. They are also left out of the
     * plugin's hierarchy, where a copied API class would otherwise look like the plugin redeclaring the member.
     * <p>
     * Reads the jar twice: first a quick pass (no method bodies) to build the plugin's own class hierarchy, so calls
     * made through a plugin subclass of an API type can be traced back to the API, then the full pass.
     */
    public static JarResult analyzeJar(Path jarPath, Inspector inspector, ClassHierarchy api) throws IOException {
        ClassHierarchy.Builder builder = ClassHierarchy.builder();
        JarUtil.forEachClass(jarPath, (entryName, classFile) -> {
            String name = className(classFile);
            if (name != null && !api.contains(name)) {
                builder.add(classFile);
            }
        });
        ClassHierarchy pluginClasses = builder.build();

        int[] classes = {0};
        int[] unreadable = {0};
        int[] bundledApi = {0};
        Map<String, Map<Deprecated, Long>> byClass = new LinkedHashMap<>();
        JarUtil.forEachClass(jarPath, (entryName, classFile) -> {
            String name = className(classFile);
            if (name == null) {
                unreadable[0]++;
                return;
            }
            if (api.contains(name)) {
                bundledApi[0]++;
                return;
            }
            try {
                new ClassReader(classFile).accept(new ClassUsage(inspector, pluginClasses), ClassReader.SKIP_FRAMES | ClassReader.SKIP_DEBUG);
                classes[0]++;
            } catch (RuntimeException e) {
                unreadable[0]++;
            }
            Map<Deprecated, Long> counts = inspector.nonZeroCountsAndReset();
            if (!counts.isEmpty()) {
                byClass.merge(name, counts, (a, b) -> {
                    Map<Deprecated, Long> merged = new LinkedHashMap<>(a);
                    b.forEach((deprecated, count) -> merged.merge(deprecated, count, Long::sum));
                    return merged;
                });
            }
        });
        return new JarResult(classes[0], unreadable[0], bundledApi[0], byClass);
    }

    /** The internal name of a class file, or null if it can't be parsed. */
    private static String className(byte[] classFile) {
        try {
            return new ClassReader(classFile).getClassName();
        } catch (RuntimeException e) {
            return null;
        }
    }

    /**
     * @param uses total references across all plugins
     * @param plugins number of plugins with at least one reference
     */
    private record Usage(Deprecated deprecated, long uses, int plugins) {
    }

    private static List<Usage> summarize(List<Deprecated> deprecateds, Map<String, Map<Deprecated, Long>> byPlugin) {
        List<Usage> usages = new ArrayList<>();
        for (Deprecated deprecated : deprecateds) {
            long uses = 0;
            int plugins = 0;
            for (Map<Deprecated, Long> counts : byPlugin.values()) {
                long count = counts.getOrDefault(deprecated, 0L);
                uses += count;
                if (count > 0) {
                    plugins++;
                }
            }
            usages.add(new Usage(deprecated, uses, plugins));
        }
        return usages;
    }

    private static void printSummary(List<Usage> usages, int pluginCount) {
        List<Usage> used = usages.stream()
            .filter(usage -> usage.uses() > 0)
            .sorted(Comparator.comparingInt(Usage::plugins).reversed().thenComparing(Comparator.comparingLong(Usage::uses).reversed()))
            .toList();
        System.out.printf("%n%d of %d deprecated entries are used by at least one of %d plugins%n",
            used.size(), usages.size(), pluginCount);
        if (used.isEmpty()) {
            return;
        }
        System.out.printf("%n%8s %6s  %s%n", "plugins", "uses", "deprecated");
        for (Usage usage : used) {
            System.out.printf("%8d %6d  %s%n", usage.plugins(), usage.uses(), describe(usage.deprecated()));
        }
    }

    private static void writeCsvs(Path outputFolder, List<Usage> usages, Map<String, Map<Deprecated, Long>> byPlugin,
                                  Map<String, Map<String, Map<Deprecated, Long>>> byClass) throws IOException {
        Files.createDirectories(outputFolder);
        try (BufferedWriter out = Files.newBufferedWriter(outputFolder.resolve("usage-by-member.csv"))) {
            out.write("type,path,owner,uses,plugins\n");
            for (Usage usage : usages) {
                out.write(String.join(",", columns(usage.deprecated())) + "," + usage.uses() + "," + usage.plugins() + "\n");
            }
        }
        try (BufferedWriter out = Files.newBufferedWriter(outputFolder.resolve("usage-by-plugin.csv"))) {
            out.write("plugin,type,path,owner,uses\n");
            for (Map.Entry<String, Map<Deprecated, Long>> plugin : byPlugin.entrySet()) {
                for (Map.Entry<Deprecated, Long> count : plugin.getValue().entrySet()) {
                    if (count.getValue() > 0) {
                        out.write(csv(plugin.getKey()) + "," + String.join(",", columns(count.getKey())) + "," + count.getValue() + "\n");
                    }
                }
            }
        }
        // which class inside each plugin makes the reference: separates the plugin's own code from bundled libraries
        // (e.g. a relocated XSeries) and per-Minecraft-version modules
        try (BufferedWriter out = Files.newBufferedWriter(outputFolder.resolve("usage-by-class.csv"))) {
            out.write("plugin,class,type,path,owner,uses\n");
            for (Map.Entry<String, Map<String, Map<Deprecated, Long>>> plugin : byClass.entrySet()) {
                for (Map.Entry<String, Map<Deprecated, Long>> clazz : plugin.getValue().entrySet()) {
                    String className = clazz.getKey().replace('/', '.');
                    for (Map.Entry<Deprecated, Long> count : clazz.getValue().entrySet()) {
                        out.write(csv(plugin.getKey()) + "," + csv(className) + "," + String.join(",", columns(count.getKey())) + "," + count.getValue() + "\n");
                    }
                }
            }
        }
    }

    /** type, path and owner in the same format as deprecated-data.csv. */
    private static List<String> columns(Deprecated deprecated) {
        return List.of(
            deprecated.deprecationType().name(),
            csv(deprecated.path().toSource()),
            deprecated.owner() == null ? "null" : csv(deprecated.owner().toSource()));
    }

    private static String describe(Deprecated deprecated) {
        return deprecated.owner() == null
            ? deprecated.path().toSource() + " (type)"
            : deprecated.owner().toSource() + "#" + deprecated.path().toSource();
    }

    private static String csv(String value) {
        if (value.indexOf(',') < 0 && value.indexOf('"') < 0) {
            return value;
        }
        return '"' + value.replace("\"", "\"\"") + '"';
    }

    private static void fail(String format, Object... args) {
        System.err.printf(format + "%n", args);
        System.exit(1);
    }
}
