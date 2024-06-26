"""Defines all the functions for creating and processing meshes."""

from typing import NamedTuple, List
import numpy as np
from scipy.spatial import transform


class TriangularMesh(NamedTuple):
    """Copied from GraphCast.

    Data structure for triangular meshes.

    Attributes:
      vertices: spatial positions of the vertices of the mesh of shape
          [num_vertices, num_dims].
      faces: triangular faces of the mesh of shape [num_faces, 3]. Contains
          integer indices into `vertices`.

    """

    vertices: np.ndarray
    faces: np.ndarray


class _ChildVerticesBuilder(object):
    """Copied from GraphCast.

    Bookkeeping of new child vertices added to an existing set of vertices.
    """

    def __init__(self, parent_vertices):

        # Because the same new vertex will be required when splitting adjacent
        # triangles (which share an edge) we keep them in a hash table indexed by
        # sorted indices of the vertices adjacent to the edge, to avoid creating
        # duplicated child vertices.
        self._child_vertices_index_mapping = {}
        self._parent_vertices = parent_vertices
        # We start with all previous vertices.
        self._all_vertices_list = list(parent_vertices)

    def _get_child_vertex_key(self, parent_vertex_indices):
        return tuple(sorted(parent_vertex_indices))

    def _create_child_vertex(self, parent_vertex_indices):
        """Creates a new vertex."""
        # Position for new vertex is the middle point, between the parent points,
        # projected to unit sphere.
        child_vertex_position = self._parent_vertices[list(parent_vertex_indices)].mean(
            0
        )
        child_vertex_position /= np.linalg.norm(child_vertex_position)

        # Add the vertex to the output list. The index for this new vertex will
        # match the length of the list before adding it.
        child_vertex_key = self._get_child_vertex_key(parent_vertex_indices)
        self._child_vertices_index_mapping[child_vertex_key] = len(
            self._all_vertices_list
        )
        self._all_vertices_list.append(child_vertex_position)

    def get_new_child_vertex_index(self, parent_vertex_indices):
        """Returns index for a child vertex, creating it if necessary."""
        # Get the key to see if we already have a new vertex in the middle.
        child_vertex_key = self._get_child_vertex_key(parent_vertex_indices)
        if child_vertex_key not in self._child_vertices_index_mapping:
            self._create_child_vertex(parent_vertex_indices)
        return self._child_vertices_index_mapping[child_vertex_key]

    def get_all_vertices(self):
        """Returns an array with old vertices."""
        return np.array(self._all_vertices_list)


def get_hierarchy_of_triangular_meshes_for_sphere(splits: int) -> List[TriangularMesh]:
    """Copied from GraphCast.

    Returns a sequence of meshes, each with triangularization sphere.

    Starting with a regular icosahedron (12 vertices, 20 faces, 30 edges) with
    circumscribed unit sphere. Then, each triangular face is iteratively
    subdivided into 4 triangular faces `splits` times. The new vertices are then
    projected back onto the unit sphere. All resulting meshes are returned in a
    list, from lowest to highest resolution.

    The vertices in each face are specified in counter-clockwise order as
    observed from the outside the icosahedron.

    Args:
       splits: How many times to split each triangle.
    Returns:
       Sequence of `TriangularMesh`s of length `splits + 1` each with:

         vertices: [num_vertices, 3] vertex positions in 3D, all with unit norm.
         faces: [num_faces, 3] with triangular faces joining sets of 3 vertices.
             Each row contains three indices into the vertices array, indicating
             the vertices adjacent to the face. Always with positive orientation
             (counterclock-wise when looking from the outside).
    """
    current_mesh = get_icosahedron()
    output_meshes = [current_mesh]
    for _ in range(splits):
        current_mesh = _two_split_unit_sphere_triangle_faces(current_mesh)
        output_meshes.append(current_mesh)
    return output_meshes


def get_icosahedron() -> TriangularMesh:
    """Copied from GraphCast

    Returns a regular icosahedral mesh with circumscribed unit sphere.

    See https://en.wikipedia.org/wiki/Regular_icosahedron#Cartesian_coordinates
    for details on the construction of the regular icosahedron.

    The vertices in each face are specified in counter-clockwise order as observed
    from the outside of the icosahedron.

    Returns:
       TriangularMesh with:

       vertices: [num_vertices=12, 3] vertex positions in 3D, all with unit norm.
       faces: [num_faces=20, 3] with triangular faces joining sets of 3 vertices.
           Each row contains three indices into the vertices array, indicating
           the vertices adjacent to the face. Always with positive orientation (
           counterclock-wise when looking from the outside).

    """
    phi = (1 + np.sqrt(5)) / 2
    vertices = []
    for c1 in [1.0, -1.0]:
        for c2 in [phi, -phi]:
            vertices.append((c1, c2, 0.0))
            vertices.append((0.0, c1, c2))
            vertices.append((c2, 0.0, c1))

    vertices = np.array(vertices, dtype=np.float32)
    vertices /= np.linalg.norm([1.0, phi])

    # I did this manually, checking the orientation one by one.
    faces = [
        (0, 1, 2),
        (0, 6, 1),
        (8, 0, 2),
        (8, 4, 0),
        (3, 8, 2),
        (3, 2, 7),
        (7, 2, 1),
        (0, 4, 6),
        (4, 11, 6),
        (6, 11, 5),
        (1, 5, 7),
        (4, 10, 11),
        (4, 8, 10),
        (10, 8, 3),
        (10, 3, 9),
        (11, 10, 9),
        (11, 9, 5),
        (5, 9, 7),
        (9, 3, 7),
        (1, 6, 5),
    ]
    angle_between_faces = 2 * np.arcsin(phi / np.sqrt(3))
    rotation_angle = (np.pi - angle_between_faces) / 2
    rotation = transform.Rotation.from_euler(seq="y", angles=rotation_angle)
    rotation_matrix = rotation.as_matrix()
    vertices = np.dot(vertices, rotation_matrix)

    return TriangularMesh(
        vertices=vertices.astype(np.float32), faces=np.array(faces, dtype=np.int32)
    )


def _two_split_unit_sphere_triangle_faces(
    triangular_mesh: TriangularMesh,
) -> TriangularMesh:
    """Copied from GraphCast.

    Splits each triangular face into 4 triangles keeping the orientation."""

    # Every time we split a triangle into 4 we will be adding 3 extra vertices,
    # located at the edge centres.
    # This class handles the positioning of the new vertices, and avoids creating
    # duplicates.
    new_vertices_builder = _ChildVerticesBuilder(triangular_mesh.vertices)

    new_faces = []
    for ind1, ind2, ind3 in triangular_mesh.faces:
        ind12 = new_vertices_builder.get_new_child_vertex_index((ind1, ind2))
        ind23 = new_vertices_builder.get_new_child_vertex_index((ind2, ind3))
        ind31 = new_vertices_builder.get_new_child_vertex_index((ind3, ind1))
        # Note how each of the 4 triangular new faces specifies the order of the
        # vertices to preserve the orientation of the original face. As the input
        # face should always be counter-clockwise as specified in the diagram,
        # this means child faces should also be counter-clockwise.
        new_faces.extend(
            [
                [ind1, ind12, ind31],  # 1
                [ind12, ind2, ind23],  # 2
                [ind31, ind23, ind3],  # 3
                [ind12, ind23, ind31],  # 4
            ]
        )
    return TriangularMesh(
        vertices=new_vertices_builder.get_all_vertices(),
        faces=np.array(new_faces, dtype=np.int32),
    )


def filter_mesh(meshes: List[TriangularMesh], mesh_levels: list[int]):
    """ Remove the faces of lower level meshes from the mesh that we want.
        Needed as graphcast creates a hierarchy of meshes and we only want the specific level.
        
        Lower levels have less faces.
    """
    mesh_levels = sorted(mesh_levels, reverse=True)
    faces: np.array = meshes[mesh_levels[0]].faces
    for level_desired in mesh_levels[1:]:
        level_mesh = meshes[level_desired]
        faces = np.concatenate((faces, level_mesh.faces), axis=0)

    mesh_we_want = TriangularMesh(vertices=meshes[mesh_levels[0]].vertices, faces=faces)
    return mesh_we_want

def get_edges_from_faces(faces) -> np.ndarray:
    """
    Get edges from faces.

    Parameters
    ----------
    faces : np.array
        The faces of the triangular mesh.

    Returns
    -------
        Returns a numpy array of shape [2, num_edges] which defines the edges.

    """
    edges = []
    for face in faces:
        edges.extend([[face[0], face[1]], [face[1], face[2]], [face[2], face[0]]])
    edges = np.array(edges).T
    edges = np.sort(edges, axis=0)  # Sort the edges from smaller number to larger number, so node pairs don't have both directions for the edges
    edges = np.unique(edges, axis=1)    # remove duplicates (due to sorting, every node pair has only one direction)

    
    # interleave edges with swapped edges to have undirected graph
    swapped_edges = np.flip(edges, axis=0)
    interleaved_edges = np.zeros((2, 2 * edges.shape[1]), dtype=edges.dtype)

    interleaved_edges[:, 0::2] = edges
    interleaved_edges[:, 1::2] = swapped_edges

    return interleaved_edges