""" Utilities for manipulation-specific Rapidly-Expanding Random Trees (RRTs). """

import numpy as np
import time

from ..core.utils import (
    check_collisions_at_state,
    check_collisions_along_path,
    configuration_distance,
    extract_cartesian_poses,
    get_random_state,
)
from ..visualization.meshcat_utils import visualize_frames, visualize_path

from .graph import Node, Graph
from .utils import discretize_joint_space_path


class RRTPlannerOptions:
    """Options for Rapidly-expanding Random Tree (RRT) planning."""

    max_angle_step = 0.05
    """ Maximum angle step, in radians, for collision checking along path segments. """

    max_connection_dist = 0.2
    """ Maximum angular distance, in radians, for connecting nodes. """

    rrt_connect = False
    """ If true, enables the RRTConnect algorithm. """

    bidirectional_rrt = False
    """
    If true, uses bidirectional RRTs from both start and goal nodes.
    Otherwise, only grows a tree from the start node.
    """

    rrt_star = False
    """
    If true, enables the RRT* algorithm.
    This in turn will use the `max_rewire_dist` parameter.
    """

    max_rewire_dist = np.inf
    """
    Maximum angular distance, in radians, to consider rewiring nodes for RRT*.
    If set to `np.inf`, all nodes in the trees will be considering for rewiring.
    """

    max_planning_time = 10.0
    """ Maximum planning time, in seconds. """

    goal_biasing_probability = 0.0
    """ Probability of sampling the goal configuration itself, which can help planning converge. """


class RRTPlanner:
    """Rapidly-expanding Random Tree (RRT) planner.

    This is a sampling-based motion planner that finds collision-free paths from a start to a goal configuration.

    Some good resources:
      * Original RRT paper: https://msl.cs.illinois.edu/~lavalle/papers/Lav98c.pdf
      * RRTConnect paper: https://www.cs.cmu.edu/afs/cs/academic/class/15494-s14/readings/kuffner_icra2000.pdf
      * RRT* and PRM* paper: https://arxiv.org/abs/1105.1186
    """

    def __init__(self, model, collision_model):
        """
        Creates an instance of an RRT planner.

        Parameters
        ----------
            model : `pinocchio.Model`
                The model to use for this solver.
            collision_model : `pinocchio.Model`
                The model to use for collision checking.
        """
        self.model = model
        self.collision_model = collision_model
        self.reset()

    def reset(self):
        """Resets all the planning data structures."""
        self.latest_path = None
        self.start_tree = Graph()
        self.goal_tree = Graph()

    def plan(self, q_start, q_goal, options=RRTPlannerOptions()):
        """
        Plans a path from a start to a goal configuration.

        Parameters
        ----------
            q_start : array-like
                The starting robot configuration.
            q_start : array-like
                The goal robot configuration.
            options : `RRTPlannerOptions`, optional
                The options to use for planning. If not specified, default options are used.
        """
        self.reset()
        t_start = time.time()
        self.options = options

        start_node = Node(q_start, parent=None, cost=0.0)
        self.start_tree.add_node(start_node)
        goal_node = Node(q_goal, parent=None, cost=0.0)
        self.goal_tree.add_node(goal_node)

        goal_found = False
        latest_start_tree_node = start_node
        latest_goal_tree_node = goal_node

        # Check start and end pose collisions.
        if check_collisions_at_state(self.model, self.collision_model, q_start):
            print("Start configuration in collision.")
            return None
        if check_collisions_at_state(self.model, self.collision_model, q_goal):
            print("Goal configuration in collision.")
            return None

        # Check direct connection to goal.
        path_to_goal = discretize_joint_space_path(
            q_start, q_goal, self.options.max_angle_step
        )
        if not check_collisions_along_path(
            self.model, self.collision_model, path_to_goal
        ):
            print("Start and goal can be directly connected!")
            goal_found = True

        start_tree_phase = True
        while not goal_found:
            # Check for timeouts.
            if time.time() - t_start > options.max_planning_time:
                print(f"Planning timed out after {options.max_planning_time} seconds.")
                break

            # Choose variables based on whether we're growing the start or goal tree.
            tree = self.start_tree if start_tree_phase else self.goal_tree
            other_tree = self.goal_tree if start_tree_phase else self.start_tree

            # Sample a new configuration.
            if np.random.random() < self.options.goal_biasing_probability:
                q_sample = q_goal if start_tree_phase else q_start
            else:
                q_sample = get_random_state(self.model)

            # Run the extend or connect operation to connect the tree to the new node.
            nearest_node = tree.get_nearest_node(q_sample)
            q_new = self.extend_or_connect(nearest_node, q_sample, options)

            # Only if extend/connect succeeded, add the new node to the tree.
            if q_new is not None:
                new_node = self.add_node_to_tree(tree, q_new, nearest_node, options)
                if start_tree_phase:
                    latest_start_tree_node = new_node
                else:
                    latest_goal_tree_node = new_node

                # Check if latest node connects directly to the other tree.
                # If so, add it to the tree and mark planning as complete.
                nearest_node_in_other_tree = other_tree.get_nearest_node(new_node.q)
                path_to_other_tree = discretize_joint_space_path(
                    new_node.q,
                    nearest_node_in_other_tree.q,
                    self.options.max_angle_step,
                )
                if not check_collisions_along_path(
                    self.model, self.collision_model, path_to_other_tree
                ):
                    new_node = self.add_node_to_tree(
                        tree, nearest_node_in_other_tree.q, new_node, options
                    )
                    if start_tree_phase:
                        latest_start_tree_node = new_node
                        latest_goal_tree_node = nearest_node_in_other_tree
                    else:
                        latest_start_tree_node = nearest_node_in_other_tree
                        latest_goal_tree_node = new_node
                    goal_found = True

                # Switch to the other tree next iteration, if bidirectional mode is enabled.
                if options.bidirectional_rrt:
                    start_tree_phase = not start_tree_phase

        # Back out the path by traversing the parents from the goal.
        self.latest_path = []
        if goal_found:
            self.latest_path = self.extract_path_from_trees(
                latest_start_tree_node, latest_goal_tree_node
            )
        return self.latest_path

    def extend_or_connect(self, parent_node, q_sample, options=RRTPlannerOptions()):
        """
        Extends a tree towards a sampled node with steps no larger than the maximum connection distance.

        Parameters
        ----------
            parent_node : `pyroboplan.planning.graph.Node`
                The node from which to start extending or connecting towards the sample.
            q_sample : array-like
                The robot configuration sample to extend or connect towards.
            options : `pyroboplan.planning.rrt.RRTPlannerOptions`, optional
                The options to use for this operation.
                These include whether to extend once or keep connecting (`options.rrt_connect`),
                as well as the maximum angular connection distance (`options.max_connection_dist`).
        """
        q_diff = q_sample - parent_node.q
        q_increment = options.max_connection_dist * q_diff / np.linalg.norm(q_diff)

        terminated = False
        q_out = None
        q_cur = parent_node.q
        while not terminated:
            # Clip the distance between nearest and sampled nodes to max connection distance.
            # If we have reached the sampled node, this is the final iteration.
            if configuration_distance(q_cur, q_sample) > options.max_connection_dist:
                q_extend = q_cur + q_increment
            else:
                q_extend = q_sample
                terminated |= True

            # Extension is successful only if the path is collision free.
            q_extend_in_collision = check_collisions_at_state(
                self.model, self.collision_model, q_extend
            )
            path_to_q_extend = discretize_joint_space_path(
                q_cur, q_extend, self.options.max_angle_step
            )
            path_to_q_extend_in_collision = check_collisions_along_path(
                self.model, self.collision_model, path_to_q_extend
            )
            if not q_extend_in_collision and not path_to_q_extend_in_collision:
                q_cur = q_out = q_extend
            else:
                terminated |= True

            # If RRTConnect is disabled, only one iteration is needed.
            if not options.rrt_connect:
                terminated |= True

        return q_out

    def extract_path_from_trees(self, start_tree_final_node, goal_tree_final_node):
        """
        Extracts the final path from the RRT trees.

        Parameters
        ----------
            start_tree_final_node : `pyroboplan.planning.graph.Node`
                The last node of the start tree.
            goal_tree_final_node : `pyroboplan.planning.graph.Node`, optional
                The last node of the goal tree.
                If None, this means the goal tree is ignored.

        Return
        ------
            list[array-like]
                A list of robot configurations describing the path waypoints in order.
        """
        path = []
        cur_node = start_tree_final_node
        path_extracted = False
        while not path_extracted:
            if cur_node is None:
                path_extracted = True
            else:
                path.append(cur_node.q)
                cur_node = cur_node.parent
        path.reverse()

        cur_node = goal_tree_final_node
        path_extracted = False
        while not path_extracted:
            if cur_node is None:
                path_extracted = True
            else:
                path.append(cur_node.q)
                cur_node = cur_node.parent

        return path

    def add_node_to_tree(self, tree, q_new, parent_node, options=RRTPlannerOptions()):
        """
        Add a new node to the tree. If the RRT* algorithm is enabled, will also rewire.
        """
        # Add the new node to the tree
        new_node = Node(q_new, parent=parent_node)
        tree.add_node(new_node)
        edge = tree.add_edge(parent_node, new_node)
        new_node.cost = parent_node.cost + edge.cost

        # If RRT* is enable it, rewire that node in the tree.
        if options.rrt_star:
            min_cost = new_node.cost
            for other_node in tree.nodes:
                # Do not consider trivial nodes.
                if other_node == new_node or other_node == parent_node:
                    continue
                # Do not consider nodes farther than the configured rewire distance,
                new_distance = configuration_distance(other_node.q, q_new)
                if new_distance > options.max_rewire_dist:
                    continue
                # Rewire if this new connections would be of lower cost and is collision free.
                new_cost = other_node.cost + new_distance
                if new_cost < min_cost:
                    new_path = discretize_joint_space_path(
                        q_new, other_node.q, options.max_angle_step
                    )
                    if not check_collisions_along_path(
                        self.model, self.collision_model, new_path
                    ):
                        new_node.parent = other_node
                        new_node.cost = new_cost
                        tree.remove_edge(edge)
                        edge = tree.add_edge(other_node, new_node)
                        min_cost = new_cost

        return new_node

    def visualize(
        self,
        visualizer,
        frame_name,
        path_name="planned_path",
        tree_name="rrt",
        show_path=True,
        show_tree=False,
    ):
        """
        Visualizes the RRT path.

        Parameters
        ----------
            visualizer : `pinocchio.visualize.meshcat_visualizer.MeshcatVisualizer`, optional
                The visualizer to use for this solver.
            frame_name : str
                The name of the frame to use when visualizing paths in Cartesian space.
            path_name : str, optional
                The name of the MeshCat component for the path.
            tree_name : str, optional
                The name of the MeshCat component for the tree.
            show_path : bool, optional
                If true, shows the final path from start to goal.
            show_tree : bool, optional
                If true, shows the entire sampled tree.
        """
        if show_path:
            q_path = []
            for idx in range(1, len(self.latest_path)):
                q_start = self.latest_path[idx - 1]
                q_goal = self.latest_path[idx]
                q_path = q_path + discretize_joint_space_path(
                    q_start, q_goal, self.options.max_angle_step
                )

            target_tforms = extract_cartesian_poses(self.model, frame_name, q_path)
            visualize_frames(
                visualizer, path_name, target_tforms, line_length=0.05, line_width=1.5
            )

        if show_tree:
            for idx, edge in enumerate(self.start_tree.edges):
                q_path = discretize_joint_space_path(
                    edge.nodeA.q, edge.nodeB.q, self.options.max_angle_step
                )
                path_tforms = extract_cartesian_poses(self.model, frame_name, q_path)
                visualize_path(
                    visualizer,
                    f"{tree_name}_start/edge{idx}",
                    path_tforms,
                    line_width=0.5,
                    line_color=[0.9, 0.0, 0.9],
                )

            for idx, edge in enumerate(self.goal_tree.edges):
                q_path = discretize_joint_space_path(
                    edge.nodeA.q, edge.nodeB.q, self.options.max_angle_step
                )
                path_tforms = extract_cartesian_poses(self.model, frame_name, q_path)
                visualize_path(
                    visualizer,
                    f"{tree_name}_goal/edge{idx}",
                    path_tforms,
                    line_width=0.5,
                    line_color=[0.0, 0.9, 0.9],
                )
