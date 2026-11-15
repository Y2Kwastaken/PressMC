// Standalone static analysis tool for plugin jars.
// Deliberately independent of the Paper fork: no paper-api dependency, no Paper repositories, no shared
// checkstyle/publishing setup (the root build excludes this project from its fork-wide configuration).
plugins {
    java
    application
}

version = "0.1.0-SNAPSHOT"

repositories {
    mavenCentral()
}

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(25)
    }
}

dependencies {
    implementation("org.ow2.asm:asm:9.9.1")

    testImplementation(platform("org.junit:junit-bom:5.13.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

application {
    mainClass = "sh.miles.pressmc.Main"
    applicationName = "static-analyzer"
}

tasks.named<JavaExec>("run") {
    // resolve relative CLI paths against the repository root rather than static-analyzer/
    workingDir = rootProject.projectDir
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = Charsets.UTF_8.name()
    options.release = 25
}

tasks.test {
    useJUnitPlatform()
}
