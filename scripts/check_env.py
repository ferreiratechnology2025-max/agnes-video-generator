import os, sys
sys.path.insert(0, r'D:\agnes-video-generator')
print('AGNES_MOCK_API:', repr(os.environ.get('AGNES_MOCK_API')))
print('AGNES_VIMAX_BRAIN:', repr(os.environ.get('AGNES_VIMAX_BRAIN')))
print('AGNES_VIMAX_I2V_TUNNEL:', repr(os.environ.get('AGNES_VIMAX_I2V_TUNNEL')))
