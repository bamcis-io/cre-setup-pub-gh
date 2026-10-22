/**
 * Networking pattern — public API barrel.
 *
 * ParameterizedVpc and RegionalNetwork (every project), the standard network stacks, and
 * PeeringMesh + CrePeeringStack (multi-region; the peering-configurator Lambda ships as a .py
 * asset under ./peering-configurator/, used by PeeringMesh via Code.fromAsset).
 */
export * from './parameterized-vpc.js';
export * from './regional-network.js';
export * from './peering-mesh.js';
export * from './stacks.js';
