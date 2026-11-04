package io.necklink.nltp;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Arrays;

/**
 * Completed inbound file, byte message, JSON payload, or urgent Giant-Fiber reflex command.
 */
public final class ReceivedItem<P> {
    private final int connId;
    private final P peerAddr;
    private final int streamId;
    private final String kind;
    private final byte[] data;
    private final String filename;
    private final Path savedPath;
    private final String sha256Hex;
    private final boolean sha256Verified;

    public ReceivedItem(
            int connId,
            P peerAddr,
            int streamId,
            String kind,
            byte[] data,
            String filename,
            Path savedPath,
            String sha256Hex,
            boolean sha256Verified
    ) {
        this.connId = connId;
        this.peerAddr = peerAddr;
        this.streamId = streamId;
        this.kind = kind;
        this.data = data;
        this.filename = filename != null ? filename : "";
        this.savedPath = savedPath;
        this.sha256Hex = sha256Hex != null ? sha256Hex : "";
        this.sha256Verified = sha256Verified;
    }

    public int connId() { return connId; }
    public P peerAddr() { return peerAddr; }
    public int streamId() { return streamId; }
    public String kind() { return kind; }
    public byte[] data() { return Arrays.copyOf(data, data.length); }
    public int byteLength() { return data.length; }
    public String filename() { return filename; }
    public Path savedPath() { return savedPath; }
    public String sha256Hex() { return sha256Hex; }
    public boolean sha256Verified() { return sha256Verified; }

    public String asText() {
        return new String(data, StandardCharsets.UTF_8);
    }
}
