package sh.miles.pressmc.walker;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.FieldVisitor;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Predicate;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Class hierarchy of a set of class files, read at runtime so the analyzer has no build dependency on them.
 * <p>
 * Needed because javac records the static receiver type as a call's owner: {@code player.getLocation()} is compiled
 * against {@code org/bukkit/entity/Player} even though {@code getLocation} is declared in {@code Entity}. Used twice:
 * <ul>
 *     <li>for the API (e.g. a paper-api jar): the Inspector registers each deprecated member under every API subtype
 *     that inherits it ({@link #inheritingMethod}, {@link #inheritingField})</li>
 *     <li>for each plugin jar: a call compiled against the plugin's own subclass of an API type
 *     ({@code class FakeClick extends InventoryClickEvent}) is traced back up to the API type it inherits the member
 *     from ({@link #externalSupertypesForMethod}, {@link #externalSupertypesForField})</li>
 * </ul>
 */
public final class ClassHierarchy {

    private static final ClassHierarchy EMPTY = new ClassHierarchy(Map.of());

    private final Map<String, ClassInfo> classes;
    private final Map<String, List<String>> subtypes;

    private ClassHierarchy(final Map<String, ClassInfo> classes) {
        this.classes = classes;
        final Map<String, List<String>> subtypes = new HashMap<>();
        for (final ClassInfo info : classes.values()) {
            if (info.superName() != null) {
                subtypes.computeIfAbsent(info.superName(), key -> new ArrayList<>()).add(info.name());
            }
            for (final String anInterface : info.interfaces()) {
                subtypes.computeIfAbsent(anInterface, key -> new ArrayList<>()).add(info.name());
            }
        }
        this.subtypes = subtypes;
    }

    /** No hierarchy: deprecated members are only matched on their declaring type. */
    public static ClassHierarchy empty() {
        return EMPTY;
    }

    /**
     * Reads every class file in an API jar or classes directory.
     *
     * @param api a jar (e.g. {@code paper-api-26.3.jar}) or a directory of class files
     */
    public static ClassHierarchy load(final Path api) throws IOException {
        final Map<String, ClassInfo> classes = new HashMap<>();
        if (Files.isDirectory(api)) {
            try (Stream<Path> files = Files.walk(api)) {
                for (final Path file : files.filter(ClassHierarchy::isClassFile).toList()) {
                    read(Files.readAllBytes(file), classes);
                }
            }
        } else {
            try (ZipFile zip = new ZipFile(api.toFile())) {
                for (final ZipEntry entry : zip.stream().filter(entry -> !entry.isDirectory() && isClassFile(entry.getName())).toList()) {
                    try (InputStream in = zip.getInputStream(entry)) {
                        read(in.readAllBytes(), classes);
                    }
                }
            }
        }
        return new ClassHierarchy(Map.copyOf(classes));
    }

    /** Builds a hierarchy from individual class files, e.g. the classes of one plugin jar. */
    public static Builder builder() {
        return new Builder();
    }

    public static final class Builder {

        private final Map<String, ClassInfo> classes = new HashMap<>();

        private Builder() {
        }

        /**
         * Adds one class file. Class files ASM can't parse (e.g. compiled for a newer Java version) are ignored.
         *
         * @return whether the class file was read
         */
        public boolean add(final byte[] classFile) {
            try {
                read(classFile, this.classes);
                return true;
            } catch (final RuntimeException e) {
                return false;
            }
        }

        public ClassHierarchy build() {
            return new ClassHierarchy(Map.copyOf(this.classes));
        }
    }

    /** Number of classes read. */
    public int size() {
        return this.classes.size();
    }

    /** Whether {@code internalName} is one of the classes read. */
    public boolean contains(final String internalName) {
        return this.classes.containsKey(internalName);
    }

    /**
     * For a method reference whose owner is one of the classes read, the nearest supertypes outside this hierarchy
     * the method may be inherited from. Walks up from {@code owner} through its superclass and interfaces, stopping at
     * any class read that declares the method (an override in the plugin's own code). Empty if {@code owner} isn't one
     * of the classes read, or declares the method itself.
     * <p>
     * E.g. for plugin classes {@code FakeClick2 extends FakeClick extends InventoryClickEvent}, a call to
     * {@code FakeClick2#setCursor} gives {@code [org/bukkit/event/inventory/InventoryClickEvent]}.
     *
     * @param parameters parameter part of the descriptor, e.g. {@code (Lorg/bukkit/inventory/ItemStack;)}
     */
    public List<String> externalSupertypesForMethod(final String owner, final String name, final String parameters) {
        return this.externalSupertypes(owner, info -> info.methods().containsKey(name + parameters));
    }

    /** Like {@link #externalSupertypesForMethod}, for a field reference. */
    public List<String> externalSupertypesForField(final String owner, final String name) {
        return this.externalSupertypes(owner, info -> info.fields().containsKey(name));
    }

    private List<String> externalSupertypes(final String owner, final Predicate<ClassInfo> declares) {
        final ClassInfo start = this.classes.get(owner);
        if (start == null || declares.test(start)) {
            return List.of();
        }
        final List<String> result = new ArrayList<>();
        final Set<String> seen = new HashSet<>();
        final Deque<String> queue = new ArrayDeque<>(supertypes(start));
        while (!queue.isEmpty()) {
            final String type = queue.poll();
            if (!seen.add(type)) {
                continue;
            }
            final ClassInfo info = this.classes.get(type);
            if (info == null) {
                result.add(type);
            } else if (!declares.test(info)) {
                queue.addAll(supertypes(info));
            }
        }
        return result;
    }

    /** Superclass first, then interfaces, so the class chain is preferred when both lead outside the hierarchy. */
    private static List<String> supertypes(final ClassInfo info) {
        final List<String> supertypes = new ArrayList<>();
        if (info.superName() != null) {
            supertypes.add(info.superName());
        }
        supertypes.addAll(info.interfaces());
        return supertypes;
    }

    /**
     * API types that inherit {@code owner}'s method {@code name + parameters} without redeclaring it, i.e. the
     * owners javac may record for a call to it. Stops descending at a type that overrides the method. Private methods,
     * constructors and static interface methods aren't inherited, so they have none.
     *
     * @param parameters parameter part of the descriptor, e.g. {@code (Lorg/bukkit/inventory/ItemStack;)}
     */
    public Set<String> inheritingMethod(final String owner, final String name, final String parameters) {
        final ClassInfo declaring = this.classes.get(owner);
        final Integer access = declaring == null ? null : declaring.methods().get(name + parameters);
        if (access == null || "<init>".equals(name) || (access & Opcodes.ACC_PRIVATE) != 0
            || ((access & Opcodes.ACC_STATIC) != 0 && declaring.isInterface())) {
            return Set.of();
        }
        return this.inheriting(owner, info -> info.methods().containsKey(name + parameters));
    }

    /**
     * API types that inherit {@code owner}'s field {@code name} without declaring a field of the same name. Private
     * fields aren't inherited, so they have none.
     */
    public Set<String> inheritingField(final String owner, final String name) {
        final ClassInfo declaring = this.classes.get(owner);
        final Integer access = declaring == null ? null : declaring.fields().get(name);
        if (access == null || (access & Opcodes.ACC_PRIVATE) != 0) {
            return Set.of();
        }
        return this.inheriting(owner, info -> info.fields().containsKey(name));
    }

    private Set<String> inheriting(final String owner, final Predicate<ClassInfo> redeclares) {
        final Set<String> result = new HashSet<>();
        final Set<String> seen = new HashSet<>();
        final Deque<String> queue = new ArrayDeque<>(this.subtypes.getOrDefault(owner, List.of()));
        while (!queue.isEmpty()) {
            final String type = queue.poll();
            if (!seen.add(type)) {
                continue;
            }
            final ClassInfo info = this.classes.get(type);
            if (info == null || redeclares.test(info)) {
                continue;
            }
            result.add(type);
            queue.addAll(this.subtypes.getOrDefault(type, List.of()));
        }
        return result;
    }

    private static boolean isClassFile(final Path path) {
        return isClassFile(path.getFileName().toString());
    }

    private static boolean isClassFile(final String name) {
        return name.endsWith(".class") && !name.endsWith("module-info.class") && !name.endsWith("package-info.class");
    }

    private static void read(final byte[] classFile, final Map<String, ClassInfo> classes) {
        new ClassReader(classFile).accept(new ClassVisitor(Opcodes.ASM9) {
            private ClassInfo info;

            @Override
            public void visit(final int version, final int access, final String name, final String signature,
                              final String superName, final String[] interfaces) {
                this.info = new ClassInfo(name, superName, interfaces == null ? List.of() : List.of(interfaces),
                    (access & Opcodes.ACC_INTERFACE) != 0, new HashMap<>(), new HashMap<>());
                classes.put(name, this.info);
            }

            @Override
            public FieldVisitor visitField(final int access, final String name, final String descriptor,
                                           final String signature, final Object value) {
                this.info.fields().put(name, access);
                return null;
            }

            @Override
            public MethodVisitor visitMethod(final int access, final String name, final String descriptor,
                                             final String signature, final String[] exceptions) {
                // synthetic bridge methods aren't source-level redeclarations
                if ((access & Opcodes.ACC_BRIDGE) == 0) {
                    this.info.methods().put(name + descriptor.substring(0, descriptor.indexOf(')') + 1), access);
                }
                return null;
            }
        }, ClassReader.SKIP_CODE | ClassReader.SKIP_DEBUG | ClassReader.SKIP_FRAMES);
    }

    /**
     * @param methods declared methods, keyed by name + parameter descriptor, to access flags
     * @param fields declared fields, keyed by name, to access flags
     */
    private record ClassInfo(String name, String superName, List<String> interfaces, boolean isInterface,
                             Map<String, Integer> methods, Map<String, Integer> fields) {
    }
}
