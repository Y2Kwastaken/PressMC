package sh.miles.pressmc.data;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public record Deprecated(DeprecatedType deprecationType, JavaType path, JavaType owner) {

    boolean hasOwner() {
        return owner != null;
    }

    public static List<Deprecated> read(Path path) {
        List<Deprecated> entries = new ArrayList<>();

        try (var reader = Files.newBufferedReader(path)) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.isBlank() || line.startsWith("#")) continue;
                if (line.equals("type,path,owner")) continue;

                List<String> split = splitCsv(line);
                if (split.size() != 3) {
                    throw new IllegalArgumentException("Expected 3 columns (type,path,owner) but got " + split.size() + ": " + line);
                }
                DeprecatedType type = DeprecatedType.valueOf(split.get(0));
                JavaType javaTypePath = JavaType.fromSource(split.get(1));
                JavaType owner;
                if (split.get(2).equals("null")) {
                    owner = null;
                } else {
                    owner = JavaType.fromSource(split.get(2));
                }

                entries.add(new Deprecated(type, javaTypePath, owner));
            }
        } catch (IOException e) {
            throw new RuntimeException(e);
        }

        return entries;
    }

    /**
     * Splits one CSV line into columns. Columns may be wrapped in double quotes so they can contain commas, e.g.
     * {@code METHOD,"disallow(org.bukkit.event.player.PlayerPreLoginEvent.Result, java.lang.String)",org.bukkit...};
     * a doubled quote ({@code ""}) inside a quoted column is a literal quote.
     */
    private static List<String> splitCsv(String line) {
        List<String> columns = new ArrayList<>();
        StringBuilder column = new StringBuilder();
        boolean quoted = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (quoted) {
                if (c == '"' && i + 1 < line.length() && line.charAt(i + 1) == '"') {
                    column.append('"');
                    i++;
                } else if (c == '"') {
                    quoted = false;
                } else {
                    column.append(c);
                }
            } else if (c == '"') {
                quoted = true;
            } else if (c == ',') {
                columns.add(column.toString());
                column.setLength(0);
            } else {
                column.append(c);
            }
        }
        if (quoted) {
            throw new IllegalArgumentException("Unclosed quote: " + line);
        }
        columns.add(column.toString());
        return columns;
    }
}
