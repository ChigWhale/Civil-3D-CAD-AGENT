using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace PipeInvertSetter
{
    /// <summary>Request as it arrives over the JSON boundary (handles or names, not ObjectIds).</summary>
    public sealed class SolveRequest
    {
        /// <summary>Structure handle (hex) or name. Required for compute/draw.</summary>
        public string? Structure { get; set; }

        /// <summary>Network handle or name; used by the context stage when no structure is given.</summary>
        public string? Network { get; set; }

        /// <summary>Minimum cover (feet). Null → pulled from each pipe's rule set.</summary>
        public double? CoverMinimum { get; set; }

        /// <summary>"MatchCrowns" (default) | "MatchInverts" | "ManualDrop".</summary>
        public string OffsetMode { get; set; } = "MatchCrowns";

        /// <summary>Drop (feet) for ManualDrop mode.</summary>
        public double ManualDrop { get; set; }

        /// <summary>Optional explicit pipe handles/names. Null → auto-discover from the structure.</summary>
        public List<string>? Pipes { get; set; }

        /// <summary>Optional pipe (handle/name) that fixes the datum at the structure.</summary>
        public string? GoverningPipe { get; set; }

        /// <summary>Optional explicit invert for the governing pipe at the structure (resolves conflicts).</summary>
        public double? StructureInvertOverride { get; set; }

        /// <summary>Sanity bound: any invert deeper than this below the surface is a failure.</summary>
        public double MaxDepthBelowSurface { get; set; } = 50.0;

        /// <summary>"auto" | "ratio" | "percent" — how rule-set slope values are interpreted.</summary>
        public string SlopeUnits { get; set; } = "auto";
    }

    public sealed class EndReport
    {
        public string Structure { get; set; } = "";
        public string StructureHandle { get; set; } = "";
        public double X { get; set; }
        public double Y { get; set; }
        public double Surface { get; set; }
        public double OldInvert { get; set; }
        public double OldCrown { get; set; }
        public double OldCover { get; set; }
        /// <summary>Highest invert that satisfies the minimum cover at this point.</summary>
        public double MaxInvertForCover { get; set; }
        public double? NewInvert { get; set; }
        public double? NewCrown { get; set; }
        public double? NewCover { get; set; }
        /// <summary>Cover as Civil 3D itself reports it after the write (null until applied).</summary>
        public double? Civil3DCoverAfterApply { get; set; }
    }

    public sealed class PipeReport
    {
        public string Name { get; set; } = "";
        public string Handle { get; set; } = "";
        public double InnerDiameter { get; set; }
        public double OuterHeight { get; set; }
        public double WallThickness { get; set; }
        public double Length2D { get; set; }
        /// <summary>"incoming" | "outgoing" | "unknown" relative to the shared structure.</summary>
        public string Flow { get; set; } = "unknown";
        /// <summary>"start" | "end" — which pipe end touches the shared structure.</summary>
        public string StructureEnd { get; set; } = "";
        public bool IsGoverning { get; set; }
        public EndReport AtStructure { get; set; } = new();
        public EndReport FarEnd { get; set; } = new();
        public double OldSlope { get; set; }
        public double? NewSlope { get; set; }
        /// <summary>Smallest cover anywhere along the pipe after the change (sampled every ~10 ft).</summary>
        public double? NewMinCoverAlongPipe { get; set; }
        /// <summary>Distance from the structure end where <see cref="NewMinCoverAlongPipe"/> occurs.</summary>
        public double? NewMinCoverAtDistance { get; set; }
        public double CoverMinimum { get; set; }
        /// <summary>"input" | "rule:&lt;key&gt;" | "pipe.MinimumCover".</summary>
        public string CoverSource { get; set; } = "";
        public double? MinSlope { get; set; }
        public double? MaxSlope { get; set; }
        public double? MaxCoverRule { get; set; }
        public string RuleSet { get; set; } = "";
        /// <summary>Every double parameter found on the pipe's rule set, raw, keyed by rule key.</summary>
        public Dictionary<string, double> RuleParams { get; set; } = new();
        /// <summary>"centerline" | "invert" — what Pipe.StartPoint.Z was detected to represent.</summary>
        public string ZConvention { get; set; } = "centerline";
        public List<string> Violations { get; set; } = new();
        /// <summary>"ok" | "conflict" | "error" | "unchanged".</summary>
        public string Status { get; set; } = "ok";
        public bool Civil3DMinCoverViolatedAfterApply { get; set; }
    }

    public sealed class ConflictReport
    {
        public string Pipe { get; set; } = "";
        public string Handle { get; set; } = "";
        /// <summary>"slope_vs_cover" | "cover_at_structure" | "sanity" | "adverse_slope".</summary>
        public string Kind { get; set; } = "";
        public string ConstraintA { get; set; } = "";
        public string ConstraintB { get; set; } = "";
        public double Delta { get; set; }
        public string Message { get; set; } = "";
        /// <summary>Machine-usable hint, e.g. a structureInvertOverride that would resolve it.</summary>
        public double? SuggestedStructureInvert { get; set; }
    }

    public sealed class StructureReport
    {
        public string Name { get; set; } = "";
        public string Handle { get; set; } = "";
        public string Network { get; set; } = "";
        public string PartsList { get; set; } = "";
        public string ReferenceSurface { get; set; } = "";
        public double X { get; set; }
        public double Y { get; set; }
        public double Rim { get; set; }
        public double Sump { get; set; }
        public double SurfaceElevation { get; set; }
        public int ConnectedPipes { get; set; }
    }

    public sealed class SolveReport
    {
        public string Handler { get; set; } = "pipe_elevation";
        public string Stage { get; set; } = "compute";
        /// <summary>True when there are no errors and no conflicts — safe to apply.</summary>
        public bool Ok { get; set; }
        public bool Applied { get; set; }
        public StructureReport Structure { get; set; } = new();
        public string OffsetMode { get; set; } = "";
        public double ManualDrop { get; set; }
        public double? CoverMinimumInput { get; set; }
        public string CoverMeasuredTo { get; set; } = "outside top of pipe (invert + innerHeight + wallThickness)";
        public string GoverningPipe { get; set; } = "";
        public double? StructureInvertOverride { get; set; }
        /// <summary>"ratio" | "percent" — how rule slopes were interpreted, and why.</summary>
        public string SlopeUnits { get; set; } = "ratio";
        public string SlopeUnitsReason { get; set; } = "";
        public List<PipeReport> Pipes { get; set; } = new();
        public List<ConflictReport> Conflicts { get; set; } = new();
        public List<string> Errors { get; set; } = new();
        public List<string> Warnings { get; set; } = new();

        public string ToJson() => JsonSerializer.Serialize(this, JsonOptions);

        public static readonly JsonSerializerOptions JsonOptions = new()
        {
            WriteIndented = true,
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
            PropertyNameCaseInsensitive = true,
        };
    }

    // ── Context (read-only overview) ─────────────────────────────────────────

    public sealed class NetworkPipeRow
    {
        public string Name { get; set; } = "";
        public string Handle { get; set; } = "";
        public string From { get; set; } = "";
        public string To { get; set; } = "";
        public double InnerDiameter { get; set; }
        public double Length2D { get; set; }
        public double StartInvert { get; set; }
        public double EndInvert { get; set; }
        public double Slope { get; set; }
        public double CoverStart { get; set; }
        public double CoverEnd { get; set; }
        public double MinCoverRule { get; set; }
        public bool MinCoverViolated { get; set; }
        public string Flow { get; set; } = "";
        public string RuleSet { get; set; } = "";
    }

    public sealed class NetworkStructureRow
    {
        public string Name { get; set; } = "";
        public string Handle { get; set; } = "";
        public double X { get; set; }
        public double Y { get; set; }
        public double Rim { get; set; }
        public double Sump { get; set; }
        public double SurfaceElevation { get; set; }
        public int ConnectedPipes { get; set; }
        public string Pipes { get; set; } = "";
    }

    // ── Run along an alignment ───────────────────────────────────────────────

    /// <summary>Request for the run solver. Give ONE of: alignment, structures (ordered), or start+end structure.</summary>
    public sealed class RunRequest
    {
        /// <summary>Alignment name or handle; structures within <see cref="OffsetTolerance"/> of it form the run.</summary>
        public string? Alignment { get; set; }
        /// <summary>Max perpendicular offset (ft) from the alignment for a structure to count as on the run.</summary>
        public double OffsetTolerance { get; set; } = 25.0;
        /// <summary>Optional network name/handle; otherwise the network with the most structures on the alignment.</summary>
        public string? Network { get; set; }
        /// <summary>Explicit ordered structure names/handles (alternative to alignment).</summary>
        public List<string>? Structures { get; set; }
        /// <summary>Start structure for a shortest-network-path run (with <see cref="EndStructure"/>).</summary>
        public string? StartStructure { get; set; }
        public string? EndStructure { get; set; }
        /// <summary>"auto" | "downstation" | "upstation" | "asListed" — which way water flows along the run.</summary>
        public string FlowDirection { get; set; } = "auto";

        public double? CoverMinimum { get; set; }
        public string OffsetMode { get; set; } = "MatchCrowns";
        public double ManualDrop { get; set; }
        /// <summary>Keep the existing invert at the upstream end of the run (tie-in).</summary>
        public bool HoldStart { get; set; }
        /// <summary>Keep the existing invert at the downstream end of the run (tie-in / outfall).</summary>
        public bool HoldEnd { get; set; }
        /// <summary>Explicit tie-in inverts keyed by structure name/handle (invert of the incoming run pipe there).</summary>
        public Dictionary<string, double>? FixedInverts { get; set; }
        public double MaxDepthBelowSurface { get; set; } = 50.0;
        public string SlopeUnits { get; set; } = "auto";
        /// <summary>Hand-set inverts applied on top of the solve (or instead of it), keyed by pipe name/handle.</summary>
        public Dictionary<string, PipeInvertOverride>? Overrides { get; set; }
    }

    public sealed class RunStructureRow
    {
        public int Index { get; set; }
        public string Name { get; set; } = "";
        public string Handle { get; set; } = "";
        public double? Station { get; set; }
        public double? Offset { get; set; }
        public double Surface { get; set; }
        public double Rim { get; set; }
        public double? OldInvertIn { get; set; }
        public double? NewInvertIn { get; set; }
        public double? OldInvertOut { get; set; }
        public double? NewInvertOut { get; set; }
        /// <summary>Incoming − outgoing invert applied by the offset rule.</summary>
        public double? Drop { get; set; }
        public double? FixedInvert { get; set; }
        /// <summary>Connected pipes that are NOT part of the run (laterals) — left untouched.</summary>
        public List<string> OtherPipes { get; set; } = new();
    }

    public sealed class RunPipeRow
    {
        public int Index { get; set; }
        public string Name { get; set; } = "";
        public string Handle { get; set; } = "";
        public string Upstream { get; set; } = "";
        public string Downstream { get; set; } = "";
        public double InnerDiameter { get; set; }
        public double Length2D { get; set; }
        public double SurfaceUp { get; set; }
        public double SurfaceDown { get; set; }
        public double OldUpInvert { get; set; }
        public double OldDownInvert { get; set; }
        public double? NewUpInvert { get; set; }
        public double? NewDownInvert { get; set; }
        public double? NewCoverUp { get; set; }
        public double? NewCoverDown { get; set; }
        public double? NewMinCoverAlongPipe { get; set; }
        public double OldSlope { get; set; }
        public double? NewSlope { get; set; }
        public double CoverMinimum { get; set; }
        public string CoverSource { get; set; } = "";
        public double? MinSlope { get; set; }
        public double? MaxSlope { get; set; }
        public double? MaxCoverRule { get; set; }
        public string RuleSet { get; set; } = "";
        public string ZConvention { get; set; } = "centerline";
        public List<string> Violations { get; set; } = new();
        public string Status { get; set; } = "ok";
        public double? Civil3DCoverUpAfterApply { get; set; }
        public double? Civil3DCoverDownAfterApply { get; set; }
        public bool Civil3DMinCoverViolatedAfterApply { get; set; }

        // Working data kept on the row so hand edits can be re-evaluated and applied
        // without re-reading the drawing. Not part of the JSON contract.
        [JsonIgnore] public double InnerHeight { get; set; }
        [JsonIgnore] public double WallThickness { get; set; }
        /// <summary>True when the pipe's Start point is at the upstream structure.</summary>
        [JsonIgnore] public bool UpIsStart { get; set; }
        /// <summary>Interior surface samples: [t from upstream (0..1), surface elevation].</summary>
        [JsonIgnore] public List<double[]> Samples { get; set; } = new();
    }

    /// <summary>Hand-entered inverts for one pipe of a run (JSON: "overrides": { "P-3": { "up": 95.1, "down": 94.6 } }).</summary>
    public sealed class PipeInvertOverride
    {
        public double? Up { get; set; }
        public double? Down { get; set; }
    }

    public sealed class RunReport
    {
        public string Handler { get; set; } = "pipe_run";
        public string Stage { get; set; } = "compute";
        public bool Ok { get; set; }
        public bool Applied { get; set; }
        public string Alignment { get; set; } = "";
        public string Network { get; set; } = "";
        public string ReferenceSurface { get; set; } = "";
        /// <summary>"downstation" | "upstation" | "asListed".</summary>
        public string FlowDirection { get; set; } = "";
        public string FlowDirectionReason { get; set; } = "";
        public string OffsetMode { get; set; } = "";
        public double ManualDrop { get; set; }
        public double? CoverMinimumInput { get; set; }
        public string CoverMeasuredTo { get; set; } = "outside top of pipe (invert + innerHeight + wallThickness)";
        public string SlopeUnits { get; set; } = "ratio";
        public string SlopeUnitsReason { get; set; } = "";
        public List<RunStructureRow> Structures { get; set; } = new();
        public List<RunPipeRow> Pipes { get; set; } = new();
        public List<ConflictReport> Conflicts { get; set; } = new();
        public List<string> Errors { get; set; } = new();
        public List<string> Warnings { get; set; } = new();

        public string ToJson() => JsonSerializer.Serialize(this, SolveReport.JsonOptions);
    }

    public sealed class ContextReport
    {
        public string Handler { get; set; } = "pipe_elevation";
        public string Stage { get; set; } = "context";
        public string Network { get; set; } = "";
        public string NetworkHandle { get; set; } = "";
        public string ReferenceSurface { get; set; } = "";
        public string PartsList { get; set; } = "";
        public StructureReport? Structure { get; set; }
        public List<string> ConnectedPipes { get; set; } = new();
        public List<NetworkStructureRow> Structures { get; set; } = new();
        public List<NetworkPipeRow> Pipes { get; set; } = new();
        public List<string> Errors { get; set; } = new();

        public string ToJson() => JsonSerializer.Serialize(this, SolveReport.JsonOptions);
    }
}
