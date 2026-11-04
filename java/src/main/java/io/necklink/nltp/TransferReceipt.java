package io.necklink.nltp;

import java.util.Locale;

/**
 * Verified transfer receipt returned by {@link NLTPClient} send operations.
 */
public final class TransferReceipt {
    private final int streamId;
    private final String filename;
    private final int bytes;
    private final String sha256;
    private final boolean verifiedSha256;
    private final double elapsedS;
    private final double goodputKbps;
    private final double estimatedLinkLossPct;
    private final boolean encrypted;

    public TransferReceipt(
            int streamId,
            String filename,
            int bytes,
            String sha256,
            boolean verifiedSha256,
            double elapsedS,
            double goodputKbps,
            double estimatedLinkLossPct,
            boolean encrypted
    ) {
        this.streamId = streamId;
        this.filename = filename != null ? filename : "";
        this.bytes = bytes;
        this.sha256 = sha256;
        this.verifiedSha256 = verifiedSha256;
        this.elapsedS = elapsedS;
        this.goodputKbps = goodputKbps;
        this.estimatedLinkLossPct = estimatedLinkLossPct;
        this.encrypted = encrypted;
    }

    public int streamId() { return streamId; }
    public String filename() { return filename; }
    public int bytes() { return bytes; }
    public String sha256() { return sha256; }
    public boolean verifiedSha256() { return verifiedSha256; }
    public double elapsedS() { return elapsedS; }
    public double goodputKbps() { return goodputKbps; }
    public double estimatedLinkLossPct() { return estimatedLinkLossPct; }
    public boolean encrypted() { return encrypted; }

    public String toJson() {
        return String.format(
                Locale.US,
                "{\"ok\":%s,\"stream_id\":%d,\"filename\":\"%s\",\"bytes\":%d,\"sha256\":\"%s\",\"verified_sha256\":%s,\"elapsed_s\":%.4f,\"goodput_kbps\":%.2f,\"estimated_link_loss_pct\":%.2f,\"encrypted\":%s}",
                verifiedSha256 ? "true" : "false",
                streamId,
                NLTPWire.jsonEscape(filename),
                bytes,
                sha256,
                verifiedSha256 ? "true" : "false",
                elapsedS,
                goodputKbps,
                estimatedLinkLossPct,
                encrypted ? "true" : "false"
        );
    }
}
