package sh.miles.pressmc.walker;

import org.objectweb.asm.Type;
import sh.miles.pressmc.data.Deprecated;
import sh.miles.pressmc.data.DeprecatedType;
import sh.miles.pressmc.data.JavaType;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.LongAdder;

/**
 * Counts references to deprecated API.
 * <p>
 * Feed it from an ASM visitor using the raw values ASM provides (internal names such as {@code org/bukkit/World} and
 * JVM descriptors). Each matching reference increments that entry's counter.
 * <p>
 * Thread safe: the set of targets is fixed at construction and the counters are {@link LongAdder}s, so one Inspector
 * can be shared by threads analyzing jars in parallel.
 */
public final class Inspector {

    private static final String CONSTRUCTOR_NAME = "<init>";

    private final Map<Key, Entry> targets;
    private final List<Entry> entries;

    /**
     * Matches deprecated members only on their declaring type. Calls compiled against a subtype (e.g. a deprecated
     * {@code InventoryClickEvent} method called on a {@code CraftItemEvent}) are missed; use
     * {@link #Inspector(List, ClassHierarchy)} to catch those.
     */
    public Inspector(final List<Deprecated> data) {
        this(data, ClassHierarchy.empty());
    }

    /**
     * @param data the deprecated API to count
     * @param hierarchy the API's class hierarchy; deprecated methods and fields are also matched on every API subtype
     *     that inherits them without overriding, since javac records the receiver's static type as the owner
     */
    public Inspector(final List<Deprecated> data, final ClassHierarchy hierarchy) {
        final Map<Key, Entry> targets = new HashMap<>();
        final List<Entry> entries = new ArrayList<>();
        for (final Deprecated datum : data) {
            final Entry entry = new Entry(datum, new LongAdder());
            final Key key = keyOf(datum);
            if (targets.putIfAbsent(key, entry) != null) {
                continue;
            }
            entries.add(entry);
            final Set<String> inheriting = switch (key.type()) {
                case METHOD -> hierarchy.inheritingMethod(key.owner(), key.name(), key.parameters());
                case FIELD -> hierarchy.inheritingField(key.owner(), key.name());
                case CONSTRUCTOR, TYPE -> Set.of();
            };
            for (final String subtype : inheriting) {
                targets.putIfAbsent(new Key(key.type(), subtype, key.name(), key.parameters()), entry);
            }
        }
        this.targets = Map.copyOf(targets);
        this.entries = List.copyOf(entries);
    }

    /**
     * A method or constructor reference, from {@code MethodVisitor#visitMethodInsn} or a method {@code Handle}
     * (method references like {@code Player::getName} arrive as handles in {@code visitInvokeDynamicInsn}).
     * <p>
     * Only the parameter part of {@code descriptor} is compared, since deprecated-data.csv has no return types.
     *
     * @param owner internal name of the class owning the method, e.g. {@code org/bukkit/World}
     * @param name method name, {@code <init>} for constructors
     * @param descriptor method descriptor, e.g. {@code ([Ljava/lang/Class;)Ljava/util/Collection;}
     * @return whether the method is deprecated (and was counted)
     */
    public boolean visitMethod(final String owner, final String name, final String descriptor) {
        final DeprecatedType type = CONSTRUCTOR_NAME.equals(name) ? DeprecatedType.CONSTRUCTOR : DeprecatedType.METHOD;
        return this.increment(new Key(type, owner, name, parameters(descriptor)));
    }

    /**
     * A field access, from {@code MethodVisitor#visitFieldInsn}.
     *
     * @param owner internal name of the class owning the field, e.g. {@code org/bukkit/map/MapPalette}
     * @param name field name
     * @return whether the field is deprecated (and was counted)
     */
    public boolean visitField(final String owner, final String name) {
        return this.increment(new Key(DeprecatedType.FIELD, owner, name, ""));
    }

    /**
     * A use of a type: e.g. {@code visitTypeInsn} ({@code new}, {@code checkcast}, {@code instanceof}), a superclass or
     * interface in {@code ClassVisitor#visit}, or the owner of a method/field call if calls on a deprecated type
     * should also count as uses of that type.
     *
     * @param internalName internal name, e.g. {@code org/bukkit/event/player/PlayerChatEvent}
     * @return whether the type is deprecated (and was counted)
     */
    public boolean visitType(final String internalName) {
        return this.increment(new Key(DeprecatedType.TYPE, internalName, "", ""));
    }

    /**
     * Every object type in a field or method descriptor, e.g. the parameter of an event listener
     * {@code onChat(Lorg/bukkit/event/player/PlayerChatEvent;)V}. Array types count as their element type.
     *
     * @param descriptor a field descriptor or a method descriptor
     * @return whether any deprecated type was counted
     */
    public boolean visitDescriptor(final String descriptor) {
        boolean hit = false;
        if (descriptor.startsWith("(")) {
            for (final Type argument : Type.getArgumentTypes(descriptor)) {
                hit |= this.visitDescriptorType(argument);
            }
            hit |= this.visitDescriptorType(Type.getReturnType(descriptor));
        } else {
            hit = this.visitDescriptorType(Type.getType(descriptor));
        }
        return hit;
    }

    /**
     * @return the current count for every deprecated entry, in the order they were given to the constructor
     */
    public Map<Deprecated, Long> counts() {
        final Map<Deprecated, Long> counts = new LinkedHashMap<>();
        for (final Entry entry : this.entries) {
            counts.put(entry.deprecated(), entry.counter().sum());
        }
        return counts;
    }

    /**
     * Like {@link #counts()}, but also resets every counter to zero, e.g. to get per-plugin counts.
     * Not atomic with respect to threads still incrementing.
     *
     * @return the counts before the reset
     */
    public Map<Deprecated, Long> countsAndReset() {
        final Map<Deprecated, Long> counts = new LinkedHashMap<>();
        for (final Entry entry : this.entries) {
            counts.put(entry.deprecated(), entry.counter().sumThenReset());
        }
        return counts;
    }

    /**
     * Like {@link #countsAndReset()}, but only returns entries with a non-zero count, e.g. to collect counts per class
     * cheaply. Not atomic with respect to threads still incrementing.
     *
     * @return the non-zero counts before the reset, in constructor order
     */
    public Map<Deprecated, Long> nonZeroCountsAndReset() {
        Map<Deprecated, Long> counts = Map.of();
        for (final Entry entry : this.entries) {
            final long count = entry.counter().sumThenReset();
            if (count != 0) {
                if (counts.isEmpty()) {
                    counts = new LinkedHashMap<>();
                }
                counts.put(entry.deprecated(), count);
            }
        }
        return counts;
    }

    private boolean visitDescriptorType(final Type type) {
        final Type element = type.getSort() == Type.ARRAY ? type.getElementType() : type;
        return element.getSort() == Type.OBJECT && this.visitType(element.getInternalName());
    }

    private boolean increment(final Key key) {
        final Entry entry = this.targets.get(key);
        if (entry == null) {
            return false;
        }
        entry.counter().increment();
        return true;
    }

    /** {@code (Ljava/lang/String;I)V} becomes {@code (Ljava/lang/String;I)}. */
    private static String parameters(final String methodDescriptor) {
        return methodDescriptor.substring(0, methodDescriptor.indexOf(')') + 1);
    }

    private static Key keyOf(final Deprecated datum) {
        final DeprecatedType type = datum.deprecationType();
        if (type == DeprecatedType.TYPE) {
            return new Key(type, internalName(datum.path()), "", "");
        }
        if (datum.owner() == null) {
            throw new IllegalArgumentException(type + " entry has no owner: " + datum);
        }
        final String owner = internalName(datum.owner());
        final String path = sourcePath(datum.path());
        if (type == DeprecatedType.FIELD) {
            return new Key(type, owner, path, "");
        }

        final int open = path.indexOf('(');
        final int close = path.lastIndexOf(')');
        if (open < 0 || close < open) {
            throw new IllegalArgumentException(type + " entry has no parameter list: " + path);
        }
        final StringBuilder parameters = new StringBuilder("(");
        for (final String parameter : path.substring(open + 1, close).split(",")) {
            if (!parameter.isBlank()) {
                parameters.append(JavaType.fromSource(parameter).jvm());
            }
        }
        parameters.append(')');
        final String name = type == DeprecatedType.CONSTRUCTOR ? CONSTRUCTOR_NAME : path.substring(0, open).strip();
        return new Key(type, owner, name, parameters.toString());
    }

    /** {@code Lorg/bukkit/World;} becomes {@code org/bukkit/World}. */
    private static String internalName(final JavaType type) {
        final String jvm = type.jvm();
        if (jvm.length() > 2 && jvm.charAt(0) == 'L' && jvm.charAt(jvm.length() - 1) == ';') {
            return jvm.substring(1, jvm.length() - 1);
        }
        throw new IllegalArgumentException("Not an object type: " + jvm);
    }

    /**
     * Recovers the CSV text for a FIELD/METHOD/CONSTRUCTOR path. {@code Deprecated.read} passes every path through
     * {@link JavaType#fromSource}, so e.g. {@code LgetData(int, int, int);} goes back to {@code getData(int, int, int)},
     * and {@code LTRANSPARENT;} back to {@code TRANSPARENT}. See {@link JavaType#toSource()}.
     */
    private static String sourcePath(final JavaType path) {
        return path.toSource();
    }

    private record Key(DeprecatedType type, String owner, String name, String parameters) {
    }

    private record Entry(Deprecated deprecated, LongAdder counter) {
    }
}
