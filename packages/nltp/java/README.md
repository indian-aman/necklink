# NLTP v1.2 — Pure Java SDK (`io.necklink:nltp`)

Zero-external-dependency **Java 11+ / Android API 26+** implementation of the **NeckLink Transport Protocol (`NLTP v1.2`)**, byte-for-byte wire-compatible with the **Python**, **ANSI C99**, **JavaScript/TypeScript**, and **Arduino / `ESP32`** stacks.

- **Zero External Runtime Dependencies**: Uses only standard `java.nio.channels.DatagramChannel`, `Selector`, and `java.security.MessageDigest` (`SHA-256`) + `CRC32` — **portable constant-time `RFC 8439` `ChaCha20-Poly1305` is built directly into `NLTPWire`**, so it works identically on **Java 11–21+ JVMs** and older **Android** runtimes without BouncyCastle.
- **Full NLTP v1.2 Wire Parity**:
  - Systematic `GF(2^8)` Cauchy forward error correction (`0x11D` primitive polynomial, `8`-shard generations, incremental Gauss-Jordan decoding).
  - `ChaCha20-Poly1305` (256-bit AEAD) + `RFC 6479` 64-packet sliding anti-replay window + strict plaintext-downgrade rejection.
  - Out-of-line urgent reflex channel (`sendReflex`) with zero head-of-line blocking.
  - 1-RTT packet-train bottleneck pacing (`DeliveryRateController` + `TokenBucketPacer`).
  - Built-in `RFC 5389` STUN discovery (`discoverWan`), UDP hole-punching (`punchP2p`), seamless IP roaming (`rebindSocket`), and zero-knowledge 1-port CGNAT relay (`NLTPRelayHub`).

---

## Maven / Gradle Coordinates & Standalone JAR

Build the zero-dependency library + CLI JAR (`--release 11`, version 1.3.2) from source with Maven, Gradle, or plain `javac` (the older `nltp-java-1.2.0.jar` on the [v1.2.0 release](https://github.com/indian-aman/necklink/releases/tag/v1.2.0) still works but predates 1.3.2):

```bash
# Maven
mvn -f java/pom.xml test package

# Gradle
gradle -p java test jar

# Direct javac (zero build-tool requirement)
javac --release 11 -Xlint:all -Werror -d java/target/classes java/src/main/java/io/necklink/nltp/*.java
```

---

## Quick Start (Java 11+)

```java
import io.necklink.nltp.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Paths;

public class QuickStart {
    public static void main(String[] args) throws Exception {
        String key = NLTPWire.generateNltpKey(); // 256-bit ChaCha20-Poly1305 key

        try (NLTPServer srv = NLTPServer.builder()
                .bind("0.0.0.0", 9000)
                .psk(key)
                .saveDir(Paths.get("./inbox"))
                .build();
             NLTPClient cli = NLTPClient.builder()
                .server("127.0.0.1", 9000)
                .psk(key)
                .maxTxKbps(10_000.0)
                .build()) {

            // 1. Out-of-line urgent reflex command (0 ms head-of-line blocking)
            cli.sendReflex("ESTOP_CHECK".getBytes(StandardCharsets.UTF_8));
            ReceivedItem<InetSocketAddress> reflex = srv.recv(2000L);
            System.out.println("Reflex: " + new String(reflex.data(), StandardCharsets.UTF_8));

            // 2. Cauchy-FEC + ChaCha20-Poly1305 + SHA-256 verified transfer
            byte[] payload = new byte[65536];
            TransferReceipt receipt = cli.sendBytes(payload, "sensor_batch.bin", 30_000L);
            System.out.println("Verified SHA-256: " + receipt.verifiedSha256()
                    + " (" + receipt.goodputKbps() + " kbps)");
        }
    }
}
```

---

## Transport-Agnostic Embedded / Custom Byte-Callback Core (`NLTPEndpoint<P>`)

`NLTPEndpoint<P>` decouples protocol framing, FEC, pacing, and encryption from sockets so you can run NLTP over Android USB-Serial, BLE L2CAP, LoRa/UHF IP modems, or WebSockets:

```java
NLTPEndpoint<String> ep = new NLTPEndpoint<>(
    (peerId, frameBytes) -> customRadio.transmit(peerId, frameBytes),
    5000.0,
    key
);
```

---

## CLI (`NLTPCli`)

```bash
java -jar target/nltp-1.3.2.jar gen-key
java -jar target/nltp-1.3.2.jar stun
java -jar target/nltp-1.3.2.jar relay --host 0.0.0.0 --port 9000
java -jar target/nltp-1.3.2.jar serve --host 0.0.0.0 --port 9000 --psk "<KEY>" --save-dir ./inbox
java -jar target/nltp-1.3.2.jar send-file --host 127.0.0.1 --port 9000 --psk "<KEY>" --file firmware.bin
```
