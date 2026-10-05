/* Which process settings OrcaSlicer greys out ("field") or hides ("line") depending on other settings.
   Transcribed from ConfigManipulation::toggle_print_fff_options in OrcaSlicer v2.4.2
   (src/slic3r/GUI/ConfigManipulation.cpp), for a single-extruder Klipper printer.
   Each rule is [what, keys, condition]; `v` reads a setting's current value as a string. */

const num = (v, key) => parseFloat(v[key]) || 0;
const on = (v, key) => v[key] === "1" || v[key] === "true";
const off = (v, key) => v[key] === "none" || v[key] === "disabled_fuzzy" || v[key] === undefined;

export const RULES = [
  // Walls
  ["field", ["enable_arc_fitting"], v => !(num(v, "max_volumetric_extrusion_rate_slope") > 0)],
  ["line", ["max_volumetric_extrusion_rate_slope_segment_length", "extrusion_rate_smoothing_external_perimeter_only"],
    v => num(v, "max_volumetric_extrusion_rate_slope") > 0],
  ["field", ["extra_perimeters_on_overhangs", "ensure_vertical_shell_thickness", "detect_thin_wall", "detect_overhang_wall", "seam_position",
    "staggered_inner_seams", "wall_sequence", "outer_wall_line_width", "inner_wall_speed", "outer_wall_speed", "small_perimeter_speed",
    "small_perimeter_threshold"], v => num(v, "wall_loops") > 0],
  ["field", ["detect_thin_wall"], v => v.wall_generator !== "arachne"],
  ["line", ["wall_transition_length", "wall_transition_filter_deviation", "wall_transition_angle", "min_feature_size", "min_length_factor",
    "min_bead_width", "wall_distribution_count", "initial_layer_min_bead_width", "wall_maximum_resolution", "wall_maximum_deviation"],
    v => v.wall_generator === "arachne"],
  ["line", ["min_width_top_surface"], v => on(v, "only_one_wall_top") || (num(v, "min_length_factor") > 0.5 && v.wall_generator === "arachne")],
  ["line", ["hole_to_polyhole_threshold", "hole_to_polyhole_twisted"], v => on(v, "hole_to_polyhole")],
  ["line", ["overhang_reverse"], v => !on(v, "spiral_mode")],
  ["line", ["overhang_reverse_internal_only"], v => !on(v, "spiral_mode") && on(v, "overhang_reverse")],
  ["line", ["overhang_reverse_threshold"], v => on(v, "detect_overhang_wall") && !on(v, "spiral_mode") && on(v, "overhang_reverse")
    && !on(v, "overhang_reverse_internal_only")],
  ["line", ["make_overhang_printable_angle", "make_overhang_printable_hole_size"], v => on(v, "make_overhang_printable")],

  // Infill
  ["line", ["sparse_infill_pattern", "infill_combination", "fill_multiline", "infill_direction", "minimum_sparse_infill_area",
    "sparse_infill_filament_id", "infill_anchor", "infill_anchor_max", "infill_shift_step", "sparse_infill_rotate_template",
    "symmetric_infill_y_axis"], v => num(v, "sparse_infill_density") > 0],
  ["line", ["infill_combination_max_layer_height"], v => on(v, "infill_combination") && num(v, "sparse_infill_density") > 0],
  ["line", ["gyroid_optimized"], v => num(v, "sparse_infill_density") > 0 && v.sparse_infill_pattern === "gyroid"],
  ["field", ["infill_anchor_max"], v => v.sparse_infill_pattern !== "line"],
  ["field", ["infill_anchor"], v => v.sparse_infill_pattern !== "line" && num(v, "infill_anchor_max") > 0],
  ["line", ["infill_shift_step"], v => ["crosszag", "lockedzag"].includes(v.sparse_infill_pattern)],
  ["line", ["skeleton_infill_density", "skin_infill_density", "infill_lock_depth", "skin_infill_depth", "skin_infill_line_width",
    "skeleton_infill_line_width"], v => v.sparse_infill_pattern === "lockedzag"],
  ["line", ["symmetric_infill_y_axis"], v => ["zigzag", "crosszag", "lockedzag"].includes(v.sparse_infill_pattern)],
  ["line", ["lateral_lattice_angle_1", "lateral_lattice_angle_2"], v => v.sparse_infill_pattern === "lateral-lattice"],
  ["line", ["lightning_overhang_angle", "lightning_prune_angle", "lightning_straightening_angle"], v => v.sparse_infill_pattern === "lightning"],
  ["line", ["infill_overhang_angle"], v => v.sparse_infill_pattern === "lateral-honeycomb"],

  // Shells
  ["line", ["spiral_mode_smooth", "spiral_starting_flow_ratio", "spiral_finishing_flow_ratio"], v => on(v, "spiral_mode")],
  ["line", ["spiral_mode_max_xy_smoothing"], v => on(v, "spiral_mode") && on(v, "spiral_mode_smooth")],
  ["field", ["top_surface_pattern", "top_surface_density", "top_surface_line_width", "top_surface_speed"],
    v => num(v, "top_shell_layers") > 0 || (on(v, "spiral_mode") && num(v, "bottom_shell_layers") > 1)],
  ["field", ["bottom_surface_pattern", "bottom_surface_density"], v => num(v, "bottom_shell_layers") > 0],
  ["field", ["infill_direction", "sparse_infill_line_width", "gap_fill_target", "filter_out_gap_fill", "infill_wall_overlap", "sparse_infill_speed",
    "bridge_speed", "internal_bridge_speed", "bridge_angle", "internal_bridge_angle", "relative_bridge_angle", "solid_infill_direction",
    "solid_infill_rotate_template", "internal_solid_infill_pattern", "internal_solid_filament_id", "top_surface_filament_id",
    "bottom_surface_filament_id"],
    v => num(v, "sparse_infill_density") > 0 || num(v, "top_shell_layers") > 0 || num(v, "bottom_shell_layers") > 0 || on(v, "spiral_mode")],
  ["field", ["top_shell_thickness"], v => !on(v, "spiral_mode") && num(v, "top_shell_layers") > 0],
  ["field", ["bottom_shell_thickness"], v => !on(v, "spiral_mode") && num(v, "bottom_shell_layers") > 0],
  ["field", ["gap_infill_speed"], v => num(v, "wall_loops") > 0],
  ["field", ["seam_slope_type"], v => !on(v, "spiral_mode")],

  // Seam slope (scarf joint)
  ["line", ["seam_slope_conditional", "seam_slope_start_height", "seam_slope_entire_loop", "seam_slope_min_length", "seam_slope_steps",
    "seam_slope_inner_walls", "scarf_joint_speed", "scarf_joint_flow_ratio"], v => !on(v, "spiral_mode") && v.seam_slope_type !== "none"],
  ["field", ["seam_slope_min_length"], v => !on(v, "seam_slope_entire_loop")],
  ["line", ["scarf_angle_threshold", "scarf_overhang_threshold"],
    v => !on(v, "spiral_mode") && v.seam_slope_type !== "none" && on(v, "seam_slope_conditional")],

  // Acceleration and jerk
  ["field", ["outer_wall_acceleration", "inner_wall_acceleration", "initial_layer_acceleration", "initial_layer_travel_acceleration",
    "top_surface_acceleration", "travel_acceleration", "bridge_acceleration", "sparse_infill_acceleration", "internal_solid_infill_acceleration"],
    v => num(v, "default_acceleration") > 0],
  ["line", ["default_junction_deviation"], () => false],  // Marlin only
  ["field", ["outer_wall_jerk", "inner_wall_jerk", "initial_layer_jerk", "initial_layer_travel_jerk", "top_surface_jerk", "travel_jerk", "infill_jerk"],
    v => num(v, "default_jerk") > 0],
  ["line", ["accel_to_decel_enable", "accel_to_decel_factor"], () => true],  // Klipper
  ["field", ["accel_to_decel_factor"], v => on(v, "accel_to_decel_enable")],

  // Skirt and brim
  ["field", ["skirt_height"], v => num(v, "skirt_loops") > 0 && v.draft_shield !== "enabled"],
  ["line", ["single_loop_draft_shield"], v => num(v, "skirt_loops") > 0],
  ["field", ["skirt_type", "min_skirt_length", "skirt_distance", "skirt_start_angle", "skirt_speed", "draft_shield"], v => num(v, "skirt_loops") > 0],
  ["field", ["brim_object_gap", "brim_use_efc_outline", "combine_brims", "brim_flow_ratio"], v => v.brim_type !== "no_brim"],
  ["field", ["brim_width"], v => !["no_brim", "auto_brim", "painted"].includes(v.brim_type)],
  ["field", ["brim_ears_max_angle", "brim_ears_detection_length"], v => num(v, "brim_width") > 0],
  ["line", ["brim_ears_max_angle", "brim_ears_detection_length"], v => v.brim_type === "brim_ears"],
  ["line", ["elefant_foot_compensation_layers"], v => num(v, "elefant_foot_compensation") > 0 || num(v, "elefant_foot_layers_density") < 100],

  // Support
  ["field", ["support_style", "support_base_pattern", "support_base_pattern_spacing", "support_expansion", "support_angle",
    "support_interface_pattern", "support_interface_top_layers", "support_interface_bottom_layers", "bridge_no_support", "max_bridge_length",
    "support_top_z_distance", "support_bottom_z_distance", "support_type", "support_on_build_plate_only", "support_critical_regions_only",
    "support_interface_not_for_body", "support_object_xy_distance", "support_object_first_layer_gap", "independent_support_layer_height",
    "raft_first_layer_density"], v => on(v, "enable_support") || num(v, "raft_layers") > 0],
  ["field", ["support_threshold_angle"], v => (on(v, "enable_support") || num(v, "raft_layers") > 0) && v.support_type?.endsWith("(auto)")],
  ["field", ["support_threshold_overlap"], v => num(v, "support_threshold_angle") === 0 && (on(v, "enable_support") || num(v, "raft_layers") > 0)
    && v.support_type?.endsWith("(auto)")],
  ["line", ["support_threshold_overlap"], v => !(on(v, "enable_support") && v.support_type?.startsWith("tree"))],
  ["line", ["tree_support_branch_angle", "tree_support_branch_distance", "tree_support_branch_diameter", "tree_support_auto_brim", "tree_support_brim_width"],
    v => on(v, "enable_support") && v.support_type?.startsWith("tree") && !["default", "organic"].includes(v.support_style)],
  ["line", ["tree_support_branch_angle_organic", "tree_support_branch_distance_organic", "tree_support_branch_diameter_organic",
    "tree_support_angle_slow", "tree_support_tip_diameter", "tree_support_top_rate", "tree_support_branch_diameter_angle"],
    v => on(v, "enable_support") && v.support_type?.startsWith("tree") && ["default", "organic"].includes(v.support_style)],
  ["line", ["independent_support_layer_height"], v => (on(v, "enable_support") || num(v, "raft_layers") > 0)
    && !(on(v, "enable_support") && v.support_type?.startsWith("tree") && ["default", "organic"].includes(v.support_style))],
  ["field", ["tree_support_brim_width"], v => on(v, "enable_support") && v.support_type?.startsWith("tree") && !on(v, "tree_support_auto_brim")],
  ["line", ["max_bridge_length"], v => on(v, "enable_support") && v.support_type?.startsWith("tree")],
  ["line", ["bridge_no_support"], v => !(on(v, "enable_support") && v.support_type?.startsWith("tree"))],
  ["line", ["support_critical_regions_only"], v => v.support_type?.endsWith("(auto)") && on(v, "enable_support") && v.support_type?.startsWith("tree")],
  ["field", ["support_interface_filament", "support_interface_loop_pattern", "support_bottom_interface_spacing"],
    v => (on(v, "enable_support") || num(v, "raft_layers") > 0) && (num(v, "support_interface_top_layers") > 0 || num(v, "support_interface_bottom_layers") > 0)],
  ["field", ["support_ironing"], v => num(v, "raft_layers") > 0 || (on(v, "enable_support") && num(v, "support_interface_top_layers") > 0)],
  ["line", ["support_ironing_pattern", "support_ironing_flow", "support_ironing_spacing"],
    v => (num(v, "raft_layers") > 0 || (on(v, "enable_support") && num(v, "support_interface_top_layers") > 0)) && on(v, "support_ironing")],
  ["field", ["support_interface_spacing"], v => (on(v, "enable_support") || num(v, "raft_layers") > 0)
    && (num(v, "support_interface_top_layers") > 0 || num(v, "support_interface_bottom_layers") > 0)
    && !((num(v, "raft_layers") > 0 || (on(v, "enable_support") && num(v, "support_interface_top_layers") > 0)) && on(v, "support_ironing"))],
  ["field", ["inner_wall_line_width"], v => num(v, "wall_loops") > 0 || num(v, "skirt_loops") > 0 || v.brim_type !== "no_brim"],
  ["field", ["support_filament"], v => on(v, "enable_support") || num(v, "raft_layers") > 0 || num(v, "skirt_loops") > 0],
  ["line", ["raft_contact_distance"], v => num(v, "raft_layers") > 0 && !((on(v, "enable_support") || num(v, "raft_layers") > 0) && num(v, "support_top_z_distance") === 0)],

  // Ironing
  ["line", ["ironing_pattern", "ironing_flow", "ironing_spacing", "ironing_angle", "ironing_inset", "ironing_angle_fixed"], v => v.ironing_type !== "no ironing"],
  ["field", ["ironing_angle", "ironing_angle_fixed"], v => v.ironing_type !== "no ironing" && v.ironing_pattern === "rectilinear"],
  ["line", ["zaa_minimize_perimeter_height", "zaa_min_z", "zaa_dont_alternate_fill_direction", "ironing_expansion"], v => on(v, "zaa_enabled")],

  // Fuzzy skin
  ["line", ["fuzzy_skin_mode", "fuzzy_skin_noise_type", "fuzzy_skin_point_distance", "fuzzy_skin_thickness", "fuzzy_skin_first_layer"], v => !off(v, "fuzzy_skin")],
  ["line", ["fuzzy_skin_scale"], v => !off(v, "fuzzy_skin") && v.fuzzy_skin_noise_type !== "classic" && v.fuzzy_skin_noise_type !== "ripple"],
  ["line", ["fuzzy_skin_octaves"], v => !off(v, "fuzzy_skin") && !["classic", "voronoi", "ripple"].includes(v.fuzzy_skin_noise_type)],
  ["line", ["fuzzy_skin_persistence"], v => !off(v, "fuzzy_skin") && ["perlin", "billow"].includes(v.fuzzy_skin_noise_type)],
  ["line", ["fuzzy_skin_ripples_per_layer", "fuzzy_skin_ripple_offset", "fuzzy_skin_layers_between_ripple_offset"],
    v => !off(v, "fuzzy_skin") && v.fuzzy_skin_noise_type === "ripple"],

  // Speed, flow and misc
  ["field", ["print_order"], v => v.print_sequence !== "by object"],
  ["field", ["wipe_speed"], v => !on(v, "role_based_wipe_speed")],
  ["line", ["first_layer_flow_ratio", "outer_wall_flow_ratio", "inner_wall_flow_ratio", "overhang_flow_ratio", "sparse_infill_flow_ratio",
    "internal_solid_infill_flow_ratio", "gap_fill_flow_ratio", "support_flow_ratio", "support_interface_flow_ratio"], v => on(v, "set_other_flow_ratios")],
  ["line", ["overhang_1_4_speed", "overhang_2_4_speed", "overhang_3_4_speed", "overhang_4_4_speed", "slowdown_for_curled_perimeters"],
    v => on(v, "enable_overhang_speed")],
  ["line", ["max_travel_detour_distance"], v => on(v, "reduce_crossing_wall")],
  ["line", ["small_area_infill_flow_compensation_model"], v => on(v, "small_area_infill_flow_compensation")],
  ["line", ["standby_temperature_delta", "preheat_time"], v => on(v, "ooze_prevention")],
  ["line", ["preheat_steps"], v => on(v, "ooze_prevention") && num(v, "preheat_steps") > 0],
  ["line", ["mmu_segmented_region_interlocking_depth"], v => !on(v, "interlocking_beam")],
  ["line", ["interlocking_beam_width", "interlocking_orientation", "interlocking_beam_layer_count", "interlocking_depth", "interlocking_boundary_avoidance"],
    v => on(v, "interlocking_beam")],

  // Prime tower
  ["line", ["prime_tower_width", "prime_tower_brim_width", "prime_tower_skip_points", "wipe_tower_wall_type", "prime_tower_infill_gap",
    "prime_tower_enable_framework", "enable_tower_interface_features"], v => on(v, "enable_prime_tower")],
  ["line", ["wipe_tower_extra_rib_length", "wipe_tower_rib_width", "wipe_tower_fillet_wall"], v => on(v, "enable_prime_tower") && v.wipe_tower_wall_type === "rib"],
  ["field", ["flush_into_infill", "flush_into_support", "flush_into_objects"], v => on(v, "enable_prime_tower")],

  // Applied last in OrcaSlicer, so they override the broader infill rule above
  ["field", ["sparse_infill_rotate_template"], v => !["adaptivecubic", "supportcubic"].includes(v.sparse_infill_pattern)],
  ["field", ["infill_direction"], v => !v.sparse_infill_rotate_template && !["adaptivecubic", "supportcubic"].includes(v.sparse_infill_pattern)],
  ["field", ["solid_infill_direction"], v => !v.solid_infill_rotate_template],
];

/* Evaluate the rules for `values` (a function key → string | undefined).
   Returns { hidden: Set of keys, disabled: Set of keys }. A key hidden or disabled by any rule stays so:
   OrcaSlicer applies its rules in order and later ones win, so the last rule for a key and kind decides. */
export function evaluate(values) {
  const v = new Proxy({}, { get: (_, key) => (typeof key === "string" ? values(key) : undefined) });
  const state = { line: new Map(), field: new Map() };
  for (const [what, keys, when] of RULES) {
    let result;
    try { result = !!when(v); } catch (_) { continue; }
    for (const key of keys) state[what].set(key, result);
  }
  const collect = map => new Set([...map].filter(([, shown]) => !shown).map(([key]) => key));
  return { hidden: collect(state.line), disabled: collect(state.field) };
}
