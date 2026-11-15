package sh.miles.pressmc.walker;

import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ConstantDynamic;
import org.objectweb.asm.FieldVisitor;
import org.objectweb.asm.Handle;
import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;

/**
 * Walks one class and reports every reference it makes to the {@link Inspector}.
 * <p>
 * What counts as a use:
 * <ul>
 *     <li>methods and constructors: {@code invoke*} instructions and method handles (method references such as
 *     {@code Player::getName} compile to a handle inside {@code invokedynamic})</li>
 *     <li>fields: {@code get*} / {@code put*} instructions and field handles</li>
 *     <li>types: superclass and interfaces, types in declared field and method signatures (e.g. an event listener
 *     parameter), {@code new} / {@code checkcast} / {@code instanceof} / array creation, class literals, caught
 *     exception types, and the owner of any method or field reference (calling a method on a deprecated type uses it)</li>
 * </ul>
 */
public class ClassUsage extends ClassVisitor {

    private final Inspector inspector;
    private final ClassHierarchy pluginClasses;

    /**
     * Without the plugin's own hierarchy: calls compiled against a plugin subclass of an API type are missed.
     */
    public ClassUsage(final Inspector inspector) {
        this(inspector, ClassHierarchy.empty());
    }

    /**
     * @param inspector receives the references
     * @param pluginClasses hierarchy of the jar being analyzed; when a member reference's owner is one of the
     *     plugin's own classes (e.g. {@code fakeClick.setCursor(...)} on {@code class FakeClick extends
     *     InventoryClickEvent}) and it doesn't override the member, the reference is attributed to the API type it
     *     inherits the member from
     */
    public ClassUsage(final Inspector inspector, final ClassHierarchy pluginClasses) {
        super(Opcodes.ASM9);
        this.inspector = inspector;
        this.pluginClasses = pluginClasses;
    }

    @Override
    public void visit(final int version, final int access, final String name, final String signature,
                      final String superName, final String[] interfaces) {
        if (superName != null) {
            this.inspector.visitType(superName);
        }
        if (interfaces != null) {
            for (final String anInterface : interfaces) {
                this.inspector.visitType(anInterface);
            }
        }
    }

    @Override
    public FieldVisitor visitField(final int access, final String name, final String descriptor, final String signature,
                                   final Object value) {
        this.inspector.visitDescriptor(descriptor);
        return null;
    }

    @Override
    public MethodVisitor visitMethod(final int access, final String name, final String descriptor, final String signature,
                                     final String[] exceptions) {
        this.inspector.visitDescriptor(descriptor);
        return new MethodUsage();
    }

    private void method(final String owner, final String name, final String descriptor) {
        // constructors aren't inherited: a super(...) call already names the API class as owner
        if (!this.inspector.visitMethod(owner, name, descriptor) && !"<init>".equals(name)) {
            final String parameters = descriptor.substring(0, descriptor.indexOf(')') + 1);
            for (final String supertype : this.pluginClasses.externalSupertypesForMethod(owner, name, parameters)) {
                if (this.inspector.visitMethod(supertype, name, descriptor)) {
                    break;
                }
            }
        }
        this.type(owner);
    }

    private void field(final String owner, final String name) {
        if (!this.inspector.visitField(owner, name)) {
            for (final String supertype : this.pluginClasses.externalSupertypesForField(owner, name)) {
                if (this.inspector.visitField(supertype, name)) {
                    break;
                }
            }
        }
        this.type(owner);
    }

    /** An internal name, or an array descriptor (method owners and type instructions can be array types). */
    private void type(final String internalNameOrArray) {
        if (internalNameOrArray.startsWith("[")) {
            this.inspector.visitDescriptor(internalNameOrArray);
        } else {
            this.inspector.visitType(internalNameOrArray);
        }
    }

    private void handle(final Handle handle) {
        if (handle.getTag() <= Opcodes.H_PUTSTATIC) {
            this.field(handle.getOwner(), handle.getName());
        } else {
            this.method(handle.getOwner(), handle.getName(), handle.getDesc());
        }
    }

    private void constant(final Object value) {
        switch (value) {
            case Type type when type.getSort() == Type.OBJECT || type.getSort() == Type.ARRAY ->
                this.inspector.visitDescriptor(type.getDescriptor());
            case Handle handle -> this.handle(handle);
            case ConstantDynamic dynamic -> {
                this.handle(dynamic.getBootstrapMethod());
                for (int i = 0; i < dynamic.getBootstrapMethodArgumentCount(); i++) {
                    this.constant(dynamic.getBootstrapMethodArgument(i));
                }
            }
            default -> {
                // strings, numbers and method types (the latter would double count lambda signatures)
            }
        }
    }

    private final class MethodUsage extends MethodVisitor {

        private MethodUsage() {
            super(Opcodes.ASM9);
        }

        @Override
        public void visitMethodInsn(final int opcode, final String owner, final String name, final String descriptor,
                                    final boolean isInterface) {
            ClassUsage.this.method(owner, name, descriptor);
        }

        @Override
        public void visitFieldInsn(final int opcode, final String owner, final String name, final String descriptor) {
            ClassUsage.this.field(owner, name);
        }

        @Override
        public void visitTypeInsn(final int opcode, final String type) {
            ClassUsage.this.type(type);
        }

        @Override
        public void visitMultiANewArrayInsn(final String descriptor, final int numDimensions) {
            ClassUsage.this.inspector.visitDescriptor(descriptor);
        }

        @Override
        public void visitLdcInsn(final Object value) {
            ClassUsage.this.constant(value);
        }

        @Override
        public void visitInvokeDynamicInsn(final String name, final String descriptor, final Handle bootstrapMethod,
                                           final Object... bootstrapMethodArguments) {
            // the bootstrap method is JDK plumbing (LambdaMetafactory, StringConcatFactory, ...); the arguments hold
            // the real target, e.g. the handle for Player::getName
            for (final Object argument : bootstrapMethodArguments) {
                ClassUsage.this.constant(argument);
            }
        }

        @Override
        public void visitTryCatchBlock(final Label start, final Label end, final Label handler, final String type) {
            if (type != null) {
                ClassUsage.this.type(type);
            }
        }
    }
}
