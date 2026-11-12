# NLTP v1.6.0 — Pure Java 11+ SDK (`io.necklink:nltp`)

Zero-external-dependency **Java 11+** implementation of the **NeckLink Transport Protocol (`NLTP v1.6.0`)**, byte-for-byte wire-compatible with the **Python**, **ANSI C99**, **JavaScript/TypeScript**, and **Arduino / `ESP32` / `ESP8266`** stacks.

- **Zero External Runtime Dependencies**: Uses only standard `java.nio.channels.DatagramChannel`, `Selector`, and `java.security.MessageDigest` (`SHA-256`) + `CRC32` + `Mac` (`HmacSHA256`) — **portable constant-time `RFC 8439` `ChaCha20-Poly1305`, `RFC 7748` `X25519` ephemeral forward secrecy, 1-RTT per-session key agreement (`NLTPSession`), and standard `LZ4` block compression/decompression are built in**, tested on **Temurin JDK 21 (`--release 11`)** without BouncyCastle *(Android device/emulator execution has not been tested yet)*.
- **Full NLTP v1.6.0 Wire Parity**:
  - Systematic `GF(2^8)` Cauchy forward error correction (`0x11D` primitive polynomial, `8`-shard generations, branchless 4×-unrolled `GF_MUL_TAB`, zero-`GF(2^8)` systematic fast-path, and incremental Gauss-Jordan decoding).
  - Built-in **standard `LZ4` raw block codec** (`cli.sendBytes(data, name, timeoutMs, true)` / `cli.sendFile(path, timeoutMs, true)` / `--compress` with smart auto-bypass on incompressible data; receivers decompress automatically and verify uncompressed `SHA-256`).
  - `ChaCha20-Poly1305` (256-bit AEAD) with `X25519 + PSK` 1-RTT ephemeral forward secrecy + `RFC 6479` 512-packet sliding anti-replay window + strict plaintext-downgrade rejection.
  - Reliable out-of-line urgent reflex channel (`sendReflex` / `waitReflexAck`) with zero head-of-line blocking.
  - Jitter- and reordering-resilient 1-RTT packet-train bottleneck pacing (`DeliveryRateController` + `TokenBucketPacer`) with BDP-bounded clean-link inflight window.
  - Built-in `RFC 5389` STUN discovery (`discoverWan`), UDP hole-punching (`punchP2p`), seamless IP roaming (`rebindSocket`), and zero-knowledge 1-port CGNAT relay (`NLTPRelayHub`).

---

## Maven / Gradle Coordinates & Standalone JAR

Build the zero-dependency library + CLI JAR (`--release 11`, version 1.6.0) from source with Maven, Gradle, or plain `javac`:

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

`NLTPEndpoint<P>` decouples protocol framing, FEC, pacing, and encryption from sockets so you can run NLTP over serial links, LoRa/UHF IP modems, WebSockets, or custom byte callbacks:

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
java -jar target/nltp-1.6.0.jar gen-key
java -jar target/nltp-1.6.0.jar stun
java -jar target/nltp-1.6.0.jar relay --host 0.0.0.0 --port 9000
java -jar target/nltp-1.6.0.jar serve --host 0.0.0.0 --port 9000 --psk "<KEY>" --save-dir ./inbox
java -jar target/nltp-1.6.0.jar send-file --host 127.0.0.1 --port 9000 --psk "<KEY>" --file telemetry.jsonl --compress
```
