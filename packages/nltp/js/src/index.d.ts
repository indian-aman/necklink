/**
 * TypeScript Declarations for NLTP v1.2 (`nltp`)
 */

export const NLTP_VERSION: number;
export const FLAG_ENCRYPTED: number;
export const HEADER_SIZE: number;
export const ENC_HEADER_SIZE: number;
export const AEAD_TAG_SIZE: number;
export const SHARD_HDR_SIZE: number;
export const DEFAULT_SHARD_BYTES: number;
export const DEFAULT_GEN_K: number;

export const PT_DATA_SHARD: number;
export const PT_CRYPTIC_PARITY: number;
export const PT_PROPRIOCEPTIVE_ACK: number;
export const PT_STREAM_META: number;
export const PT_STREAM_FIN_ACK: number;
export const PT_REFLEX_MSG: number;
export const PT_NEURAL_STATE: number;
export const PT_PING: number;
export const PT_PONG: number;
export const PT_REFLEX_ACK: number;

export interface PeerAddress {
  host: string;
  port: number;
  family?: number | string;
}

export interface UnpackedFrame {
  pktType: number;
  flags: number;
  connId: number;
  tsMs: number;
  payload: Uint8Array;
}

export interface StunDiscoveryResult {
  publicIp: string;
  publicPort: number;
  localPort: number;
  rttMs: number;
  endpointIndependentNat: boolean;
  natSummary: string;
  serversQueried: string[];
}

export interface TransferReceipt {
  streamId: number;
  stream_id: number;
  filename: string;
  bytes: number;
  sha256: string;
  verifiedSha256: boolean;
  verified_sha256: boolean;
  elapsedS: number;
  elapsed_s: number;
  goodputKbps: number;
  goodput_kbps: number;
  estimatedLinkLossPct: number;
  estimated_link_loss_pct: number;
  encrypted: boolean;
}

export function generateNltpKey(): string;
export function deriveAeadKey(psk: string | Uint8Array | ArrayBuffer): Uint8Array;
export function sha256Bytes(data: string | Uint8Array | ArrayBuffer): Uint8Array;
export function sha256Hex(data: string | Uint8Array | ArrayBuffer): string;
export function crc32Ieee(buf: Uint8Array, initCrc?: number): number;
export function gf256Mul(a: number, b: number): number;
export function gf256Inv(a: number): number;
export function cauchyRow(parityOffset: number, kShards: number): Uint8Array;
export function makeRoomTag(roomName: string): Uint8Array;

export class ReplayWindow64 {
  highestSeq: bigint;
  bitmap: bigint;
  wouldAccept(seq: number | bigint): boolean;
  accept(seq: number | bigint): boolean;
}

export class AEADCipherContext {
  readonly key: Uint8Array;
  constructor(psk: string | Uint8Array);
  nextSeq(): bigint;
  seal(connId: number, seq64: bigint, aadHdr20: Uint8Array, plaintext: Uint8Array): Uint8Array;
  openAndCheckReplay(peerConnId: number, seq64: bigint, aadHdr20: Uint8Array, ciphertextWithTag: Uint8Array): Uint8Array | null;
}

export function packNltpFrame(
  pktType: number,
  connId: number,
  payload: Uint8Array | string,
  opts?: { flags?: number; tsMs?: number | null; aead?: AEADCipherContext | null }
): Uint8Array;

export function unpackNltpFrame(raw: Uint8Array, aead?: AEADCipherContext | null): UnpackedFrame | null;

export class ReceivedItem {
  connId: number;
  peerAddr: PeerAddress;
  peer: PeerAddress;
  streamId: number;
  kind: 'bytes' | 'json' | 'file' | 'reflex' | string;
  data: Uint8Array;
  filename: string;
  savedPath: string | null;
  sha256Hex: string;
  sha256Verified: boolean;
  asText(): string;
  asJson<T = unknown>(): T;
}

export class NLTPEndpoint {
  connId: number;
  srttMs: number;
  minRttMs: number;
  peerLossEma: number;
  wireBytesTx: number;
  wireBytesRx: number;
  authDropsRx: number;
  roamEvents: number;
  constructor(opts: {
    connId?: number | null;
    sendPacketFn: (raw: Uint8Array, peer: PeerAddress | [string, number] | string) => void;
    onFileSaveFn?: ((safeName: string, bytes: Uint8Array) => string | null) | null;
    windowGenerations?: number;
    shardBytes?: number;
    genK?: number;
    psk?: string | Uint8Array | null;
    maxTxKbps?: number | null;
    keepaliveS?: number;
    adaptiveRate?: boolean;
    initialBurstBytes?: number | null;
  });
  startTransfer(peer: PeerAddress | [string, number] | string, data: Uint8Array | string, opts?: { kind?: string; filename?: string; nowS?: number | null }): any;
  sendReflex(peer: PeerAddress | [string, number] | string, msg: Uint8Array | string, opts?: { crypticTwin?: boolean; nowS?: number | null }): number;
  /** True once the receiver acknowledged reflex `seq` (peers older than v1.3.2 never acknowledge). */
  reflexAcked(seq: number): boolean;
  /** Reflexes still being retransmitted while waiting for an acknowledgement. */
  reflexPending(): number;
  reflexRetransmits: number;
  reflexAcksRx: number;
  reflexExpired: number;
  sendPing(peer: PeerAddress | [string, number] | string, nowS?: number | null): void;
  stepTx(nowS?: number | null): void;
  step(nowS?: number | null): void;
  popReceived(): ReceivedItem | null;
  onDatagram(raw: Uint8Array, peer: PeerAddress | [string, number] | string, nowS?: number | null): void;
  waitInbox(timeoutMs?: number): Promise<ReceivedItem | null>;
}

export interface NLTPServerOptions {
  host?: string;
  port?: number;
  saveDir?: string | null;
  psk?: string | Uint8Array | null;
  relayAddr?: PeerAddress | [string, number] | null;
  room?: string | null;
  maxTxKbps?: number | null;
  keepaliveS?: number;
  lossDropSim?: number;
  adaptiveRate?: boolean;
  initialBurstBytes?: number | null;
}

export class NLTPServer {
  engine: NLTPEndpoint;
  constructor(opts?: NLTPServerOptions);
  static listen(opts?: NLTPServerOptions): Promise<NLTPServer>;
  ready(): Promise<void>;
  address(): PeerAddress;
  recv(timeoutMs?: number): Promise<ReceivedItem | null>;
  sendBytes(clientAddr: PeerAddress | [string, number], data: Uint8Array | string, opts?: { filename?: string; timeoutMs?: number }): Promise<boolean>;
  sendFile(clientAddr: PeerAddress | [string, number], filepath: string, opts?: { timeoutMs?: number }): Promise<boolean>;
  discoverWan(opts?: { stunServers?: [string, number][]; timeoutMs?: number }): Promise<StunDiscoveryResult>;
  punchP2p(peerWanAddr: PeerAddress | [string, number], opts?: { timeoutMs?: number; prePunchTtl?: number }): Promise<boolean>;
  close(): Promise<void>;
  waitReflexAck(seq: number, timeoutMs?: number): Promise<boolean>;
  sendReflex(clientAddr: PeerAddress, urgentMsg: string | Uint8Array): Promise<number>;
}

export interface NLTPClientOptions extends NLTPServerOptions {
  serverAddr?: PeerAddress | [string, number] | null;
  bindHost?: string | null;
  bindPort?: number;
}

export class NLTPClient {
  engine: NLTPEndpoint;
  serverAddr: PeerAddress;
  constructor(opts: NLTPClientOptions);
  static connect(opts: NLTPClientOptions): Promise<NLTPClient>;
  ready(): Promise<void>;
  address(): PeerAddress;
  setPeerEndpoint(host: string, port: number): Promise<void>;
  rebindSocket(opts?: { host?: string | null; port?: number }): Promise<PeerAddress>;
  sendBytes(data: Uint8Array | string, opts?: { filename?: string; timeoutMs?: number }): Promise<TransferReceipt>;
  sendMessage(text: string, opts?: { timeoutMs?: number }): Promise<TransferReceipt>;
  sendJson(obj: unknown, opts?: { timeoutMs?: number }): Promise<TransferReceipt>;
  sendFile(filepath: string, opts?: { timeoutMs?: number }): Promise<TransferReceipt>;
  sendReflex(urgentMsg: string | Uint8Array): Promise<number>;
  recv(timeoutMs?: number): Promise<ReceivedItem | null>;
  discoverWan(opts?: { stunServers?: [string, number][]; timeoutMs?: number }): Promise<StunDiscoveryResult>;
  punchP2p(peerWanAddr: PeerAddress | [string, number], opts?: { timeoutMs?: number; prePunchTtl?: number }): Promise<boolean>;
  close(): Promise<void>;
  waitReflexAck(seq: number, timeoutMs?: number): Promise<boolean>;
}

export class NLTPRelayHub {
  forwardedPackets: number;
  forwardedBytes: number;
  readonly relayedPackets: number;
  readonly relayedBytes: number;
  constructor(opts?: { host?: string; port?: number; peerTtlS?: number });
  static listen(opts?: { host?: string; port?: number; peerTtlS?: number }): Promise<NLTPRelayHub>;
  ready(): Promise<void>;
  address(): PeerAddress;
  close(): Promise<void>;
}

export function discoverWanEndpoint(opts?: { stunServers?: [string, number][]; timeoutMs?: number }): Promise<StunDiscoveryResult>;
