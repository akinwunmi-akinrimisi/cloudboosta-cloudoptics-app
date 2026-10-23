/** Plain names for the recommendation rules, shared by the list, the detail and Settings. */
export const RULES: Record<string, string> = {
  R1_unattached_ebs: 'Unattached volume',
  R1_old_snapshot: 'Old snapshot',
  R2_idle_eip: 'Idle Elastic IP',
  R2_idle_lb: 'Idle load balancer',
  R3_gp2_gp3: 'gp2 to gp3',
  R4_idle_ec2: 'Idle server',
  R4_idle_rds: 'Unused database',
  R5_rightsize_ec2: 'Smaller server size',
  R6_newer_generation: 'Older server type',
  R6_graviton: 'Move to Graviton (Arm)',
}
