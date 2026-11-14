from __future__ import annotations

import unittest

from subtitle_pipeline.serve_cli import build_server_command, select_engine


class ServeCliTests(unittest.TestCase):
    def test_cuda_hosts_select_vllm(self):
        self.assertEqual(
            select_engine(cuda=True, system="linux", machine="x86_64", mlx=False),
            "vllm",
        )

    def test_apple_silicon_selects_mlx_when_installed(self):
        self.assertEqual(
            select_engine(cuda=False, system="darwin", machine="arm64", mlx=True),
            "mlx",
        )
        self.assertEqual(
            select_engine(cuda=False, system="darwin", machine="arm64", mlx=False),
            "external",
        )

    def test_other_hosts_use_an_external_endpoint(self):
        self.assertEqual(
            select_engine(cuda=False, system="linux", machine="x86_64", mlx=False),
            "external",
        )

    def test_vllm_command_contains_the_served_model(self):
        command = build_server_command(
            "vllm",
            python="python",
            model_path="/models/primary",
            model="vendor/model",
            host="127.0.0.1",
            port=8123,
            gpu_memory_utilization=0.92,
            max_model_len=4096,
        )
        self.assertEqual(command[0], "python")
        self.assertIn("vllm.entrypoints.openai.api_server", command)
        self.assertIn("vendor/model", command)
        self.assertIn("8123", command)

    def test_mlx_command_uses_the_server_module(self):
        command = build_server_command(
            "mlx",
            python="python",
            model_path="/models/primary",
            model="vendor/model",
            host="127.0.0.1",
            port=8123,
            gpu_memory_utilization=0.92,
            max_model_len=4096,
        )
        self.assertIn("mlx_lm.server", command)
        self.assertIn("/models/primary", command)
        self.assertIn("8123", command)

    def test_external_engine_has_no_local_command(self):
        with self.assertRaises(ValueError):
            build_server_command(
                "external",
                python="python",
                model_path="/models/primary",
                model="vendor/model",
                host="127.0.0.1",
                port=8123,
                gpu_memory_utilization=0.92,
                max_model_len=4096,
            )


if __name__ == "__main__":
    unittest.main()
