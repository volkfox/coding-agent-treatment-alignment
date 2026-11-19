"""A local stand-in for HarnessRouter, with scripted agents. Plan section 6, "Stub" and "Personas".

    from rj.stub.server import serve
    server = serve(port=0)            # http://127.0.0.1:<server.port>; server.shutdown() when done

    python -m rj.stub.server --port 8089

server.py is the HTTP side (standard library only); agent.py plays the personas in personas/*.yaml.
"""
