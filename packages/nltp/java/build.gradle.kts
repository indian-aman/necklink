plugins {
    `java-library`
    application
}

group = "io.necklink"
version = "1.6.0"

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
}

application {
    mainClass.set("io.necklink.nltp.NLTPCli")
}

tasks.jar {
    manifest {
        attributes["Main-Class"] = "io.necklink.nltp.NLTPCli"
    }
}

tasks.register<JavaExec>("selfTest") {
    group = "verification"
    description = "Runs the zero-dependency NLTP Java cryptographic, FEC, UDP & CGNAT relay test suite."
    classpath = sourceSets["test"].runtimeClasspath
    mainClass.set("io.necklink.nltp.NLTPSelfTest")
}
