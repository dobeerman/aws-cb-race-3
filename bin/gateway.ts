import { App } from 'aws-cdk-lib';
import { GatewayStack } from '../lib/gateway-stack';

const app = new App();
new GatewayStack(app, 'SpeedRunAiGateway', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-1' },
});
